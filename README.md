# Chess Sensei

A local web app that reviews your chess games like a human coach, not an engine.
Stockfish finds the mistakes and better moves; Claude explains *why*, using only facts
the engine and python-chess have verified. See [PLAN.md](PLAN.md) for the full plan.

## Status

| Phase | What | State |
|---|---|---|
| 1 | PGN upload, Stockfish analysis, move classification, review page | ✅ Done |
| 2 | Coach explanations, game summary, follow-up questions | ✅ Done |
| 3 | Personal weakness profile + puzzles from your own mistakes | ⏳ Next |
| 4 | Learning from chess-book PDFs | Not started |

## Features
- Paste or upload PGNs (several games per file). Bad games are rejected with the game and move that failed.
- Background Stockfish analysis with progress, an eval cache, and Lichess-style move classification
  (best, excellent, good, book, inaccuracy, mistake, blunder, missed win).
- Review page: board with move badges and arrows (red = played, green = best), evaluation bar,
  clickable eval graph, move list with badges, arrow-key navigation, and a player picker to say
  which side you were.
- Coach panel for each mistake: your likely intention, why it fails, the opponent's punishment,
  the better move and its idea, and the principle to remember. "Show line" buttons play the lines
  on the board.
- Game review: turning points, how the game was decided, a strength, the main weakness and one
  thing to practise.
- Follow-up questions about any position; moves you mention are checked by Stockfish first.

## Setup (WSL / Ubuntu, Python 3.11+)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit values
```

### Stockfish
Either `sudo apt install stockfish` (path `/usr/games/stockfish`), or use the official
binary from https://stockfishchess.org/download/ and point `STOCKFISH_PATH` at it.

## Run

```bash
uvicorn app.main:app --reload
# open http://localhost:8000
```

Paste or upload a PGN (several games per file is fine). Games whose players match
`MY_USERNAMES` get your colour set automatically; otherwise click your name in the games table
or on the review page. Open a game and click **Get coach review** for explanations
(needs `ANTHROPIC_API_KEY`).

## Tests

```bash
pytest
```

Engine tests (real Stockfish: blunder detection on fixtures, eval cache, 40-move timing)
are skipped automatically if `STOCKFISH_PATH` doesn't point at a working engine.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/games` | Upload PGN text (`pgn`) and/or a file (`file`); `analyze=true` to start analysis |
| GET | `/api/games` | List games (filters: `result`, `opening`, `color`) |
| GET / PATCH / DELETE | `/api/games/{id}` | Game with moves, coach notes and summary / set your colour / delete |
| POST | `/api/games/{id}/analyze` | Start engine analysis (background) |
| GET | `/api/games/{id}/status` | Analysis progress |
| POST | `/api/games/{id}/coach` | Coach notes for all mistakes + game summary (background; `refresh=true` to redo) |
| GET | `/api/games/{id}/coach/status` | Coaching progress |
| GET | `/api/moves/{id}/coach` | Coach note for one move (generated on demand) |
| POST | `/api/moves/{id}/ask` | Follow-up question about a position |

## Config (`.env`)

Copy [.env.example](.env.example) to `.env` and edit. Settings are read at startup, so restart
the server after changing them. Engine and threshold changes only apply to games you analyse
(or re-analyse) afterwards.

### Engine (Stockfish)

| Variable | Default | How it works |
|---|---|---|
| `STOCKFISH_PATH` | `/usr/games/stockfish` | Path to the Stockfish binary. Analysis and follow-up questions need it. |
| `ENGINE_DEPTH` | `18` | How deep Stockfish searches each position. Higher = stronger but slower. |
| `ENGINE_TIME` | `0.5` | Max seconds per position. Search stops at the depth **or** the time limit, whichever comes first, so a 40-move game takes roughly 30–60 s. |
| `ENGINE_MULTIPV` | `3` | How many top moves are stored per position. They show under "Top engine moves" and are given to the coach as alternatives. |
| `ENGINE_THREADS` | `4` | CPU threads Stockfish may use. Set to your core count or less. |
| `ENGINE_HASH_MB` | `256` | Memory for Stockfish's search table. More helps a little at higher depths. |

Each position's result is cached in the database, so re-analysing a game or analysing
a game that shares an opening is faster. Cached results are reused only if they were made
at the same or greater `ENGINE_DEPTH`.

### Coach (Claude)

| Variable | Default | How it works |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Your Claude API key. Needed only for coach explanations, game reviews and questions. |
| `COACH_MODEL` | `claude-sonnet-5-5` | Model that writes the explanations. |
| `COACH_EFFORT` | `medium` | How hard the model thinks: `low`, `medium`, `high`, `xhigh`, `max`. Higher can give more careful explanations but is slower and costs more. |
| `COACH_FALLBACKS` | `true` | If the model declines a request, it is re-run on a fallback model automatically. Only used with models that support it. |
| `COACH_ONLY_MY_MOVES` | `true` | When your colour is known, only your mistakes are explained (fewer API calls). Set to `false` to also explain the opponent's. |

Cost: one API call per mistake plus one for the game summary (sometimes one extra call
if the coach mentions a move that isn't in the engine data and has to rewrite). Notes are
saved, so opening a game again costs nothing; "Regenerate review" asks again.

### You

| Variable | Default | How it works |
|---|---|---|
| `MY_USERNAMES` | – | Your usernames, comma-separated (e.g. `me_lichess,me_chesscom`). Matched case-insensitively against the PGN's White/Black names to set your colour; otherwise pick it in the UI. |
| `PLAYER_RATING` | `1200` | Your approximate rating. The coach pitches its explanations at this level. |

### App

| Variable | Default | How it works |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./chess_coach.db` | Where games, analysis, the eval cache and coach notes are stored. |

### Classification thresholds (optional)

Not in `.env.example`; add them only to change how strict the labels are. Values are the
drop in winning chances (in %) caused by a move.

| Variable | Default |
|---|---|
| `INACCURACY_THRESHOLD` | `10` |
| `MISTAKE_THRESHOLD` | `20` |
| `BLUNDER_THRESHOLD` | `30` |

## How moves are classified
- Evals are stored from White's point of view and converted to win % with the Lichess formula
  (forced mate = 100% / 0%).
- `drop = win% before − win% after` for the side that moved: ≥10 inaccuracy, ≥20 mistake, ≥30 blunder.
- **Missed win**: the position was winning (≥80%) and the move dropped it below 60%.
- Not flagged when the game was already decided (both sides of the move above 95% or below 5%).
- Moves following a line in [app/data/book.txt](app/data/book.txt) are **book**.

## How the coach stays accurate
- Stockfish and python-chess decide what's true; the LLM only explains.
- For each mistake, [facts.py](app/services/facts.py) computes verified facts (hanging pieces,
  forks, pins, skewers, discovered attacks, mate threats, trapped pieces, ignored threats,
  king safety, pawn structure, development, activity) and sends them with the engine lines.
- The reply comes back as structured JSON. Every move it mentions is checked against the data;
  unknown moves trigger one retry, and anything still unverified is removed.
- Follow-up questions: moves you mention are checked by Stockfish before the coach answers.
- Notes are cached in the database; "Regenerate review" asks again.

## License

[GPL-3.0](LICENSE). The app uses [python-chess](https://github.com/niklasf/python-chess) (GPL-3.0)
and is designed to run with [Stockfish](https://stockfishchess.org/) (GPL-3.0, not included).
Front-end libraries loaded from a CDN: cm-chessboard (MIT), chess.js (BSD-2), Chart.js (MIT).
The thumbs-up badge icon is from Material Icons (Apache-2.0).
