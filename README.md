# Chess Sensei

A local web app that reviews your chess games like a human coach, not an engine.
Stockfish finds the mistakes and better moves; an LLM explains *why*, using only facts
the engine and python-chess have verified. Use Claude, any OpenAI-compatible API, or a
free model running on your own computer. See [PLAN.md](PLAN.md) for the full plan.

## Status

| Phase | What | State |
|---|---|---|
| 1 | PGN upload, Stockfish analysis, move classification, review page | ✅ Done |
| 2 | Coach explanations, game summary, follow-up questions | ✅ Done |
| 3 | Personal weakness profile + puzzles from your own mistakes | 🚧 In progress (3.1 weakness tags done) |
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
- Weakness tags on every mistake (hanging piece, allowed fork, missed mate, ignored threat, …),
  worked out from verified facts, so they work without an LLM.

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
(needs an LLM, see [Choosing an LLM](#choosing-an-llm)). Everything else works without one.

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
| `ENGINE_TIME` | `0.5` | Max seconds per position in the quick pass. Search stops at the depth **or** the time limit, whichever comes first. |
| `ENGINE_DEEP_DEPTH` | `24` | Depth for the second, deeper look at mistakes (see below). |
| `ENGINE_DEEP_TIME` | `3` | Max seconds per position in that deeper look. `0` turns it off. |
| `ENGINE_MULTIPV` | `3` | How many top moves are stored per position. They show under "Top engine moves" and are given to the coach as alternatives. |
| `ENGINE_THREADS` | `4` | CPU threads Stockfish may use. Set to your core count or less. |
| `ENGINE_HASH_MB` | `256` | Memory for Stockfish's search table. More helps a little at higher depths. |

**Two passes.** Every position first gets a quick search. A short search can misjudge
sharp positions, so the positions before and after every mistake (and every near-miss)
are then searched again, deeper, and those moves are re-classified. Labels end up as
reliable as a deep search while most of the game stays fast: a typical 40-move game
takes about 1–2 minutes.

**Cache.** Each position's result is saved, with the depth the search actually reached and
the time it was allowed. A saved result is reused when it had at least the same budget, so
re-analysing is fast; raising `ENGINE_DEPTH`/`ENGINE_TIME` makes positions get searched again.

### Coach (LLM)

| Variable | Default | How it works |
|---|---|---|
| `COACH_PROVIDER` | `anthropic` | `anthropic` for Claude, or `openai` for any OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter, Ollama, LM Studio). |
| `COACH_MODEL` | `claude-sonnet-5-5` | Model name, as the provider spells it. |
| `ANTHROPIC_API_KEY` | – | Claude API key (provider `anthropic`). |
| `COACH_API_KEY` | – | API key for provider `openai`. Leave empty for local servers. |
| `COACH_BASE_URL` | – | Server URL for provider `openai`. Empty means OpenAI itself. |
| `COACH_EFFORT` | `medium` | Claude only. How hard the model thinks: `low` … `max`. Higher can be more careful but is slower and costs more. |
| `COACH_FALLBACKS` | `true` | Claude only. If a request is declined, it is re-run on a fallback model automatically. |
| `COACH_ONLY_MY_MOVES` | `true` | When your colour is known, only your mistakes are explained (fewer calls). `false` also explains the opponent's. |

Cost: one call per mistake plus one for the game summary (sometimes one extra call
if the coach mentions a move that isn't in the engine data and has to rewrite). Notes are
saved, so opening a game again costs nothing; "Regenerate review" asks again.

### Choosing an LLM

Pick one block and put it in `.env`. Model names are examples; use any model the provider offers.

**Free, on your own computer — [Ollama](https://ollama.com)** (no key, no internet needed after download):
```
COACH_PROVIDER=openai
COACH_BASE_URL=http://localhost:11434/v1
COACH_MODEL=qwen2.5:14b
```
Download the model once first with `ollama pull qwen2.5:14b`.
**Free, local — [LM Studio](https://lmstudio.ai)**: start its local server, then
`COACH_PROVIDER=openai`, `COACH_BASE_URL=http://localhost:1234/v1`, `COACH_MODEL=<loaded model name>`.

**Free tier — Google Gemini** ([get a key](https://aistudio.google.com/apikey)):
```
COACH_PROVIDER=openai
COACH_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
COACH_API_KEY=your-gemini-key
COACH_MODEL=gemini-2.5-flash
```
**Free tier — Groq**: `COACH_BASE_URL=https://api.groq.com/openai/v1`, a Groq key, and e.g.
`COACH_MODEL=llama-3.3-70b-versatile`.

**OpenRouter** (many models, some free): `COACH_BASE_URL=https://openrouter.ai/api/v1`, an
OpenRouter key, and a model id from their site.

**OpenAI**: `COACH_PROVIDER=openai`, `COACH_API_KEY=sk-…`, leave `COACH_BASE_URL` empty.

**Claude** (default): `COACH_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=…`, `COACH_MODEL=claude-sonnet-5-5`.

Quality depends on the model. The facts and the move check keep any model from inventing
moves, but small local models (under ~7B) write weaker explanations and may fail to return
the required JSON; 14B+ local models or the cloud options work much better. The app asks the
server for a JSON schema response, and falls back to plain JSON mode for servers that don't
support schemas.

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
- Stockfish and python-chess decide what's true; the LLM only explains. This works the same
  with every provider.
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
Python SDKs used for the coach: `anthropic` (MIT) and `openai` (Apache-2.0).
Front-end libraries loaded from a CDN: cm-chessboard (MIT), chess.js (BSD-2), Chart.js (MIT).
The thumbs-up badge icon is from Material Icons (Apache-2.0).
