# Chess Coach App — Build Plan

## Instructions for Claude (read first)
- Build this project **phase by phase**. Finish one phase, make sure its acceptance criteria pass, then **stop and summarise** before starting the next one.
- Keep the code simple and readable. Don't add features that aren't in the current phase.
- Environment: WSL Ubuntu, Python 3.11+. Use a `venv` and `requirements.txt`.
- Never hardcode secrets. Read them from `.env` (`ANTHROPIC_API_KEY`, `STOCKFISH_PATH`, `COACH_MODEL`).
- Write tests for the analysis and fact-extraction logic (pytest). Use known positions and PGNs as fixtures.
- If something in this plan is ambiguous or wrong, ask before guessing.

---

## 1. Goal
A local web app that reviews my chess games like a **human chess coach**, not an engine:
- I paste or upload a PGN (one or more games).
- Stockfish finds my mistakes and the better moves.
- An LLM explains **in detail** why each mistake was bad, what the opponent can do now, why the alternative is better, and the general principle behind it.
- Over time it learns my recurring weaknesses and drills me on positions from my own games.
- Later: it can learn from chess-book PDFs and teach from them.

**Core rule: Stockfish and python-chess decide what's true; the LLM only explains facts it was given.** The LLM must never invent variations or evaluations.

---

## 2. Tech stack
| Layer | Choice |
|---|---|
| Backend | FastAPI, Uvicorn |
| Chess logic | `python-chess` (PGN parsing, board, legality, attacks, pins) |
| Engine | Stockfish 17 binary via UCI (`chess.engine.SimpleEngine`) — `sudo apt install stockfish` or the official binary; path in `.env` |
| LLM | Anthropic Python SDK (`anthropic`), model from `COACH_MODEL` env var |
| Database | SQLite + SQLModel |
| Frontend | Static HTML/JS served by FastAPI (no build step): `chessground` or `cm-chessboard` for the board, `chess.js` for move navigation, Chart.js for the eval graph |
| Tests | pytest |

---

## 3. Project structure
```
chess-sensei/
├── app/
│   ├── main.py              # FastAPI app, routes mounted, static files
│   ├── config.py            # settings from .env (pydantic-settings)
│   ├── db.py                # engine/session, create tables
│   ├── models.py            # SQLModel tables
│   ├── schemas.py           # Pydantic request/response models
│   ├── routes/
│   │   ├── games.py
│   │   ├── analysis.py
│   │   ├── coach.py
│   │   ├── profile.py
│   │   └── puzzles.py
│   ├── services/
│   │   ├── pgn_service.py       # parse/validate PGN, split multi-game, detect my color
│   │   ├── engine_service.py    # Stockfish wrapper, eval cache
│   │   ├── classify.py          # win% + move classification
│   │   ├── facts.py             # position fact extraction (tactics, structure, safety)
│   │   ├── coach_service.py     # prompt building, LLM calls, output validation
│   │   ├── profile_service.py   # weakness aggregation
│   │   └── puzzle_service.py    # puzzles from my mistakes + spaced repetition
│   └── static/
│       ├── index.html
│       ├── review.html
│       ├── css/
│       └── js/
├── tests/
│   ├── fixtures/            # sample PGNs, FENs
│   ├── test_pgn.py
│   ├── test_classify.py
│   └── test_facts.py
├── .env.example
├── requirements.txt
└── README.md
```

---

## 4. Data model (SQLite)
- **Game**: id, pgn_raw, white, black, white_elo, black_elo, result, date, event, time_control, opening_eco, opening_name, my_color, created_at, analysis_status (`pending|running|done|error`), analysis_progress (0–100)
- **Move**: id, game_id, ply, move_number, color, san, uci, fen_before, fen_after, eval_before_cp, eval_after_cp, mate_before, mate_after, win_pct_before, win_pct_after, best_move_san, best_move_uci, pv_best (SAN list, JSON), pv_played (refutation line, JSON), multipv (top 3 lines with evals, JSON), classification (`best|excellent|good|inaccuracy|mistake|blunder|missed_win|book`), phase (`opening|middlegame|endgame`), facts (JSON)
- **EvalCache**: fen (PK), depth, multipv (JSON), created_at — so the same position is never analysed twice
- **CoachNote**: id, move_id, explanation (JSON, structured — see §7), model, created_at
- **GameSummary**: id, game_id, summary (JSON), created_at
- **Puzzle** (phase 3): id, move_id, fen, solution_uci (JSON line), theme_tags, ease_factor, interval_days, due_date, times_seen, times_correct

---

## 5. Analysis pipeline
### 5.1 PGN ingestion
- Accept pasted text or an uploaded `.pgn` file; split files with multiple games.
- Validate with `chess.pgn.read_game`. Reject a game with errors and report which game and which move failed.
- Detect my colour: match `MY_USERNAMES` (comma-separated in `.env`) against the White/Black headers; otherwise let the user pick in the UI.

### 5.2 Engine analysis
- For every position: Stockfish with `multipv=3`, limit `depth=18` (configurable) or `time=0.5s`, whichever comes first.
- Store evals from **White's point of view**; convert to the mover's point of view when classifying.
- Run as a FastAPI background task; update `analysis_progress` so the UI can poll `GET /api/games/{id}/status`.
- Reuse a single engine process for the whole game; close it cleanly.

### 5.3 Classification (win-percentage based, like Lichess)
- `win% = 50 + 50 * (2 / (1 + exp(-0.00368208 * cp)) - 1)`; a forced mate counts as ±100%.
- `drop = win%_before - win%_after`, from the mover's point of view.
- Thresholds (configurable): inaccuracy ≥ 10, mistake ≥ 20, blunder ≥ 30.
- `missed_win`: the best move kept a winning position (win% ≥ 80) and the played move dropped it below 60.
- Don't flag moves that are only "bad" in already-lost or already-won positions (e.g. drop happens entirely above 95% or below 5%).
- Opening moves found in a small ECO/book list → `book`, not flagged.
- Phase: opening until move 10–12 or until most minor pieces are developed; endgame when non-pawn material is ≤ 13 points per side or queens are off with low material.

### 5.4 Fact extraction (`facts.py`) — what makes the coaching accurate
For each flagged move, compute and store as JSON:
- **Material** before/after, plus the material change along the refutation line.
- **Hanging pieces**: my pieces attacked and undefended, or attacked by a lower-value piece — before vs after my move ("your move left the knight on e5 undefended").
- **What the opponent's best reply does**: capture, check, fork (one piece attacking 2+ of value ≥ 3, or king plus a piece), pin/skewer (`board.is_pinned`, line attacks), discovered attack, mate threat (`board.is_checkmate()` within the PV), trapped piece.
- **Threats I ignored**: use a null move before my move to see what the opponent was already threatening.
- **What the best move achieves**: tactic it starts, threat it creates or parries, material it wins along the PV.
- **King safety**: castled or not, pawn shield intact, open/half-open files next to the king, number of attackers near the king.
- **Pawn structure changes**: isolated, doubled, backward and passed pawns created or removed.
- **Development** (opening only): undeveloped minor pieces, king still in the centre, early queen moves.
- **Piece activity**: mobility difference for the moved piece; pieces on the rim.
- Every line converted to **SAN** with move numbers.

---

## 6. API endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/games` | Upload PGN text or file → list of created game ids |
| GET | `/api/games` | List games (filter by result, opening, colour) |
| GET | `/api/games/{id}` | Game + moves + classifications |
| POST | `/api/games/{id}/analyze` | Start engine analysis (background) |
| GET | `/api/games/{id}/status` | Analysis progress |
| POST | `/api/games/{id}/coach` | Generate coach notes for all flagged moves + game summary |
| GET | `/api/moves/{id}/coach` | Coach note for one move (generate on demand if missing) |
| POST | `/api/moves/{id}/ask` | Follow-up question about a position ("why not Bxf7?") — that move gets engine-checked first |
| GET | `/api/profile` | Aggregated weaknesses (phase 3) |
| GET | `/api/puzzles/due` · POST `/api/puzzles/{id}/attempt` | Drills (phase 3) |

---

## 7. LLM coach design
### 7.1 System prompt (summary of the rules)
- You are an experienced, encouraging chess coach. Your student is rated around `{rating}`.
- You will receive **verified** engine data and position facts. Base every claim on them.
- **Only mention moves and lines that appear in the provided data.** Never invent variations, never state an evaluation that wasn't given.
- Explain in human terms: plans, ideas, threats, principles. Use numbers sparingly ("you go from roughly equal to losing a piece", not "−2.31").
- Guess the student's likely intention behind the move, then explain why it doesn't work.
- Be specific and concrete. Refer to squares and pieces.

### 7.2 Input per mistake (JSON)
Game context (opening, my colour, ratings, time control), move number, FEN, played move, classification, win% before/after, best move + PV, refutation PV after the played move, the other 2 MultiPV lines, all facts from §5.4, the previous 3 moves for context.

### 7.3 Output (structured JSON — use tool use / JSON mode)
```json
{
  "headline": "One-line summary of the mistake",
  "likely_intention": "What you were probably trying to do",
  "why_it_fails": "Detailed explanation, citing the refutation line",
  "opponent_punishment": "What the opponent can do now, with the line",
  "better_move": "SAN",
  "why_better": "The idea behind the better move and what it achieves",
  "alternatives": [{"move": "SAN", "comment": "..."}],
  "principle": "General lesson to remember",
  "theme_tags": ["hanging_piece", "missed_fork", "king_safety", "..."]
}
```

### 7.4 Hallucination guard (must have)
- After the LLM responds, pull out every SAN-looking move with a regex and check that it appears in the supplied lines **or** is legal and engine-verified from the given FEN.
- If an unknown move is found, retry once with a correction message; if it still fails, drop that sentence or field and log it.
- Follow-up questions (`/ask`): before answering, run Stockfish on any move the user mentions and give that evaluation to the LLM.

### 7.5 Game summary
Input: all flagged moves + their coach notes + the eval graph. Output: the 2–3 turning points, how the game was won or lost, a strength shown, the main weakness, and **one** concrete thing to practise.

### 7.6 Cost control
- Only call the LLM for inaccuracies and worse (not good moves).
- Cache coach notes in the DB; don't regenerate unless asked.
- Batch: one call per mistake (better quality) + one call for the summary. Model configurable.

---

## 8. Frontend (review page)
- Board on the left with arrows: red = my move, green = best move. Step through moves with the arrow keys.
- Eval graph under the board (Chart.js); click a point to jump to that move. Mistakes marked as coloured dots.
- Move list with classification badges (?!, ?, ??).
- Coach panel on the right: the structured explanation for the current move, "Show line" buttons that play the PV on the board, and a box to ask a follow-up question.
- Game summary card at the top.
- Home page: PGN paste box + file upload + list of past games.

---

## 9. Phases & acceptance criteria

### Phase 1 — PGN + engine review ✅ Done
- PGN upload (paste/file, multiple games), validation with clear errors, my colour detected.
- Stockfish analysis in the background with progress, eval cache, classification.
- Review page: board, move navigation, eval graph, badges, best-move arrows.
- **Done when:** I can upload a 40-move game, analysis finishes in under ~1–2 min, blunders are flagged correctly on test fixtures, and the UI shows them.

### Phase 2 — Coach explanations ✅ Done
- `facts.py` with tests on known tactical positions (fork, pin, hanging piece, back-rank mate).
- Coach service: prompt, structured output, hallucination guard, caching.
- Game summary + follow-up questions.
- **Done when:** every flagged move has a detailed explanation that only mentions verified moves, and fact-extraction tests pass.

### Phase 3 — Personal profile + training 🚧 In progress (milestone 3.1 of 5 done)
- Aggregate theme tags, phases, openings and results across games → profile page ("you blunder most in the middlegame; 40% of your mistakes are hanging pieces").
- The coach gets the profile so it can point out recurring patterns in new reviews.
- Puzzles from my own mistakes (find the best move from the position before my error), SM-2 spaced repetition, multi-move solutions validated against the engine PV.
- Optional: import games by username from the Lichess API (`/api/games/user/{username}`) and the Chess.com public API.
- **Done when:** after 10+ games the profile shows meaningful stats and puzzles come back on a schedule.

### Phase 4 — Learn from chess books (later) — Not started
- Upload a PDF → extract text with PyMuPDF (scanned books need OCR, e.g. Tesseract — flag it if no text layer is found).
- Chunk by chapter/section, embed, and store in a vector store (sqlite-vec or Chroma).
- When explaining a mistake, retrieve passages related to its theme tags and cite them ("Silman calls this…").
- "Teach me chapter X" mode: summarise the chapter, then quiz me using positions from my own games that show the same theme.
- Note: diagrams in PDFs are images, so turning them into FENs is a separate, harder problem — keep it out of scope at first.

---

## 10. Config (`.env.example`)
```
ANTHROPIC_API_KEY=
COACH_MODEL=claude-sonnet-5-5
STOCKFISH_PATH=/usr/games/stockfish
ENGINE_DEPTH=18
ENGINE_MULTIPV=3
ENGINE_THREADS=4
ENGINE_HASH_MB=256
MY_USERNAMES=your_username
PLAYER_RATING=1200
DATABASE_URL=sqlite:///./chess_coach.db
```

## 11. Run
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values
uvicorn app.main:app --reload
# open http://localhost:8000
```

**Status:** Phases 1 and 2 are done. Phase 3: milestone 3.1 (weakness tags) done; next is 3.2 (profile page).