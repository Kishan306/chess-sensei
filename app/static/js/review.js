import { Chessboard, COLOR, FEN } from "https://cdn.jsdelivr.net/npm/cm-chessboard@8.7.8/src/Chessboard.js";
import { Arrows, ARROW_TYPE } from "https://cdn.jsdelivr.net/npm/cm-chessboard@8.7.8/src/extensions/arrows/Arrows.js";
import { Chess } from "https://cdn.jsdelivr.net/npm/chess.js@1.0.0/dist/esm/chess.js";
import { el, playerPicker } from "./players.js";
import { BADGE_STYLE, MoveBadge, badgeIcon } from "./badges.js";

const ASSETS = "https://cdn.jsdelivr.net/npm/cm-chessboard@8.7.8/assets/";
const ARROW_PLAYED = ARROW_TYPE.danger; // red
const ARROW_BEST = { class: "arrow-best", slice: "arrowDefault", headSize: 7 }; // green

const LABELS = {
  best: "Best move", excellent: "Excellent", good: "Good", book: "Book move",
  inaccuracy: "Inaccuracy", mistake: "Mistake", blunder: "Blunder", missed_win: "Missed win",
};
const LIST_BADGES = ["inaccuracy", "mistake", "blunder", "missed_win", "best", "book"]; // shown in the move list
const FLAGGED = ["inaccuracy", "mistake", "blunder", "missed_win"];
const DOT_COLORS = Object.fromEntries(FLAGGED.map((c) => [c, BADGE_STYLE[c].color]));

const gameId = location.pathname.split("/").pop();
let game = null;
let ply = 0; // 0 = start position, i = after move i
let board = null;
let chart = null;
let orientation = COLOR.white;
let line = null; // when set, the board is showing a line instead of the game
const answers = {}; // move id -> [{question, answer}]
const loadingNotes = new Set(); // move ids being explained right now

// ---------- formatting ----------

function formatEval(cp, mate) {
  if (mate !== null && mate !== undefined) {
    if (mate === 0) return "#";
    return mate > 0 ? `+M${mate}` : `-M${-mate}`;
  }
  if (cp === null || cp === undefined) return "–";
  const p = cp / 100;
  return (p > 0 ? "+" : "") + p.toFixed(2);
}

/** SAN list → "12... Nf6 13. Bg5 h6" starting from a FEN. */
function formatLine(sans, fen) {
  if (!sans || !sans.length) return "";
  const parts = fen.split(" ");
  let white = parts[1] === "w";
  let num = parseInt(parts[5], 10);
  const out = [];
  sans.forEach((san, i) => {
    if (white) out.push(`${num}. ${san}`);
    else out.push(i === 0 ? `${num}... ${san}` : san);
    if (!white) num += 1;
    white = !white;
  });
  return out.join(" ");
}

function moveLabel(m) {
  return `${m.move_number}${m.color === "white" ? "." : "..."} ${m.san}`;
}

const normalizeMove = (s) => s.replace(/[\s+#]/g, "");

// ---------- players + summary ----------

function renderPlayers() {
  const box = document.getElementById("players");
  const hint = el("span", { class: "muted" }, game.my_color ? "You played:" : "Which player are you?");
  box.replaceChildren(hint, playerPicker(game, (color) => {
    hint.textContent = color ? "You played:" : "Which player are you?";
    renderSummary();
    renderMoveInfo();
    if (color) {
      orientation = color === "black" ? COLOR.black : COLOR.white;
      board.setOrientation(orientation);
      updateEvalBar();
    }
  }));
}

function renderSummary() {
  const box = document.getElementById("summary");
  const sides = ["white", "black"].map((color) => {
    const moves = game.moves.filter((m) => m.color === color);
    const count = (c) => moves.filter((m) => m.classification === c).length;
    const name = color === "white" ? game.white : game.black;
    const isMe = game.my_color === color;
    return el("div", {},
      el("div", { class: isMe ? "me" : "" }, `${color === "white" ? "♔" : "♚"} ${name || color}${isMe ? " (you)" : ""}`),
      el("div", { class: "side-stats" },
        el("span", { class: "c-inaccuracy" }, `${count("inaccuracy")} inaccuracies`),
        el("span", { class: "c-mistake" }, `${count("mistake")} mistakes`),
        el("span", { class: "c-blunder" }, `${count("blunder")} blunders`),
        el("span", { class: "c-missed_win" }, `${count("missed_win")} missed wins`)));
  });
  const info = el("div", { class: "muted" },
    [game.opening_eco, game.opening_name].filter(Boolean).join(" ") || "Unknown opening",
    el("br"), `Result: ${game.result}${game.time_control ? ` · ${game.time_control}` : ""}`);
  box.replaceChildren(...sides, info);
}

function plyForLabel(label) {
  const target = normalizeMove(label);
  const m = game.moves.find((mv) => normalizeMove(moveLabel(mv)) === target);
  return m ? m.ply : null;
}

function renderCoachReview() {
  const box = document.getElementById("coach-review");
  const s = game.summary;

  if (game.coach_status === "running") {
    box.replaceChildren(
      el("p", {}, `Your coach is reviewing the game… ${game.coach_progress}%`),
      el("div", { class: "progress" }, el("div", { style: `width:${game.coach_progress}%` })),
      el("p", { class: "muted" }, "This takes a few seconds per mistake."));
    return;
  }

  const startButton = (label, refresh = false) => el("button", {
    class: refresh ? "" : "primary",
    onclick: async () => {
      if (!game.my_color && !refresh &&
          !confirm("You haven't said which player you are, so the coach will explain both sides' mistakes. Continue?")) return;
      const res = await fetch(`/api/games/${gameId}/coach${refresh ? "?refresh=true" : ""}`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alert(body.detail || "Couldn't start the coach review.");
        return;
      }
      game.coach_status = "running";
      game.coach_progress = 0;
      renderCoachReview();
      pollCoach();
    },
  }, label);

  if (!s) {
    const parts = [
      el("p", {}, "Get a coach's explanation of every mistake plus a summary of the game."),
      startButton("Get coach review"),
    ];
    if (game.coach_status === "error") parts.unshift(el("p", { class: "error" }, game.coach_error || "Coach review failed."));
    box.replaceChildren(...parts);
    return;
  }

  const turning = el("ol", { class: "turning" }, ...s.turning_points.map((tp) => {
    const target = plyForLabel(tp.move);
    return el("li", {},
      target ? el("button", { class: "link", onclick: () => goTo(target) }, tp.move) : el("strong", {}, tp.move),
      " — ", tp.description);
  }));
  box.replaceChildren(
    el("div", { class: "review-grid" },
      el("div", {}, el("h3", {}, "Turning points"), turning),
      el("div", {},
        el("h3", {}, "How it was decided"), el("p", {}, s.how_it_was_decided),
        el("h3", {}, "What you did well"), el("p", {}, s.strength_shown),
        el("h3", {}, "Main weakness"), el("p", {}, s.main_weakness))),
    el("div", { class: "practice" }, el("strong", {}, "Practise next: "), s.practice_next),
    game.coach_error ? el("p", { class: "muted" }, game.coach_error) : "",
    el("div", { class: "row" }, startButton("Regenerate review", true)));
}

async function pollCoach() {
  const st = await (await fetch(`/api/games/${gameId}/coach/status`)).json();
  game.coach_status = st.status;
  game.coach_progress = st.progress;
  game.coach_error = st.error;
  if (st.status === "running") {
    renderCoachReview();
    setTimeout(pollCoach, 2000);
    return;
  }
  const fresh = await (await fetch(`/api/games/${gameId}`)).json();
  game.notes = fresh.notes;
  game.summary = fresh.summary;
  renderCoachReview();
  renderMoveInfo();
}

// ---------- move list ----------

function renderMoveList() {
  const rows = [];
  for (let i = 0; i < game.moves.length; i += 2) {
    const w = game.moves[i];
    const b = game.moves[i + 1];
    rows.push(el("tr", {},
      el("td", { class: "num" }, `${w.move_number}.`),
      el("td", {}, moveCell(w)),
      el("td", {}, b ? moveCell(b) : "")));
  }
  // Games from a custom start position aren't supported, so moves[0] is always White.
  document.getElementById("movelist").replaceChildren(el("table", {}, el("tbody", {}, ...rows)));
}

function moveCell(m) {
  const icon = LIST_BADGES.includes(m.classification) ? badgeIcon(m.classification, 15) : null;
  return el("span", { class: "mv", "data-ply": m.ply, title: LABELS[m.classification] || "", onclick: () => goTo(m.ply) },
    m.san, icon || "");
}

// ---------- move panel: coach + engine details + questions ----------

/** Win % for the side that moved → words. */
function describe(pct) {
  if (pct >= 90) return "winning";
  if (pct >= 70) return "clearly better";
  if (pct >= 55) return "slightly better";
  if (pct > 45) return "about equal";
  if (pct > 30) return "slightly worse";
  if (pct > 10) return "clearly worse";
  return "losing";
}

/** One plain sentence from engine data, shown until the coach has explained the move. */
function plainSummary(m) {
  const mine = (pct) => (m.color === "white" ? pct : 100 - pct);
  const before = describe(mine(m.win_pct_before));
  const after = describe(mine(m.win_pct_after));
  const who = m.color === game.my_color ? "You were" : `${m.color === "white" ? "White" : "Black"} was`;
  const parts = [before === after
    ? `${who} ${before} before ${m.san} and still ${after} after it, but gave away a lot of the advantage.`
    : `${who} ${before} before ${m.san} and ${after} after it.`];
  if (m.best_move_san) parts.push(`Stronger was ${m.best_move_san}.`);
  if (m.pv_played && m.pv_played.length) parts.push(`The key reply now is ${m.pv_played[0]}.`);
  return parts.join(" ");
}

function lineButton(label, fen, sans, kind) {
  if (!sans || !sans.length) return "";
  return el("button", { class: "show-line", onclick: () => startLine(label, fen, sans, kind) }, `▶ ${label}`);
}

function coachSection(m) {
  const note = game.notes?.[m.id];
  if (!note) {
    if (loadingNotes.has(m.id)) {
      return el("div", { class: "coach" }, el("p", { class: "muted" }, "Your coach is thinking… (about 10–30 seconds)"));
    }
    return el("div", { class: "coach" },
      el("p", { class: "plain" }, plainSummary(m)),
      el("button", { class: "primary", onclick: () => explainMove(m) }, "Explain this move"));
  }
  const refutation = lineButton("Show the punishment", m.fen_after, m.pv_played, "danger");
  const best = lineButton("Show the better line", m.fen_before, m.pv_best, "best");
  return el("div", { class: "coach" },
    el("p", { class: "coach-headline" }, note.headline),
    note.likely_intention ? el("div", {}, el("h4", {}, "What you were going for"), el("p", {}, note.likely_intention)) : "",
    note.why_it_fails ? el("div", {}, el("h4", {}, "Why it doesn't work"), el("p", {}, note.why_it_fails)) : "",
    note.opponent_punishment ? el("div", {}, el("h4", {}, "What the opponent can do now"), el("p", {}, note.opponent_punishment), refutation) : refutation,
    el("div", {},
      el("h4", {}, `Better was ${note.better_move || m.best_move_san}`),
      note.why_better ? el("p", {}, note.why_better) : "", best),
    note.alternatives?.length ? el("div", {}, el("h4", {}, "Other options"),
      el("ul", {}, ...note.alternatives.map((a) => el("li", {}, el("strong", {}, a.move), " — ", a.comment)))) : "",
    note.principle ? el("div", { class: "principle" }, el("strong", {}, "Remember: "), note.principle) : "",
  );
}

async function explainMove(m) {
  loadingNotes.add(m.id);
  renderMoveInfo();
  try {
    const res = await fetch(`/api/moves/${m.id}/coach`);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "The coach couldn't explain this move.");
    game.notes = { ...(game.notes || {}), [m.id]: body };
  } catch (err) {
    alert(err.message);
  } finally {
    loadingNotes.delete(m.id);
    if (ply > 0 && game.moves[ply - 1].id === m.id) renderMoveInfo();
  }
}

function askSection(m) {
  const list = el("div", { class: "answers" }, ...(answers[m.id] || []).map((qa) =>
    el("div", { class: "qa" }, el("p", { class: "q" }, qa.question), el("p", { class: qa.error ? "error" : "" }, qa.answer))));
  const input = el("textarea", { rows: "2", placeholder: "Ask the coach about this position, e.g. “Why not Bxf7?”" });
  const button = el("button", { onclick: async () => {
    const question = input.value.trim();
    if (!question) return;
    button.disabled = true;
    button.textContent = "Thinking…";
    let entry;
    try {
      const res = await fetch(`/api/moves/${m.id}/ask`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }),
      });
      const body = await res.json();
      entry = res.ok ? { question, answer: body.answer } : { question, answer: body.detail || "Something went wrong.", error: true };
    } catch (err) {
      entry = { question, answer: String(err.message || err), error: true };
    }
    (answers[m.id] ||= []).push(entry);
    if (ply > 0 && game.moves[ply - 1].id === m.id) {
      renderMoveInfo();
      const box = document.getElementById("move-info");
      box.scrollTop = box.scrollHeight; // show the new answer
    }
  } }, "Ask");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); button.click(); }
  });
  return el("div", { class: "ask" }, el("h4", {}, "Ask a follow-up question"), list, input, el("div", { class: "row" }, button));
}

function engineDetails(m) {
  const cls = m.classification;
  const flagged = FLAGGED.includes(cls);
  return el("details", { class: "engine" },
    el("summary", {}, "Engine details"),
    el("dl", {},
      el("dt", {}, "Evaluation"),
      el("dd", {}, `${formatEval(m.eval_before_cp, m.mate_before)} → ${formatEval(m.eval_after_cp, m.mate_after)}`,
        el("span", { class: "muted" }, `  (White win chance ${Math.round(m.win_pct_before)}% → ${Math.round(m.win_pct_after)}%)`)),
      ...(cls !== "best" && cls !== "book" && m.best_move_san ? [
        el("dt", {}, "Best line"),
        el("dd", { class: "line" }, formatLine(m.pv_best, m.fen_before)),
      ] : []),
      ...(flagged && m.pv_played?.length ? [
        el("dt", {}, "Opponent's best reply"),
        el("dd", { class: "line" }, formatLine(m.pv_played, m.fen_after)),
      ] : []),
      ...(m.multipv?.length > 1 ? [
        el("dt", {}, "Top engine moves"),
        el("dd", {}, el("ol", {}, ...m.multipv.map((l) =>
          el("li", { class: "line" }, `${formatEval(l.cp, l.mate)}  ${formatLine(l.san.slice(0, 6), m.fen_before)}`)))),
      ] : [])));
}

/** Weakness tags: verified fact tags first, then any extra tags from the coach. */
function tagRow(m) {
  const tags = [...(m.tags || [])];
  for (const t of game.notes?.[m.id]?.theme_tags || []) if (!tags.includes(t)) tags.push(t);
  if (!tags.length) return "";
  return el("div", { class: "tags" }, ...tags.map((t) => el("span", { class: "tag" }, t.replace(/_/g, " "))));
}

function renderMoveInfo() {
  const box = document.getElementById("move-info");
  const scroll = box.scrollTop;
  if (ply === 0) {
    box.replaceChildren(el("div", { class: "headline" }, "Starting position"),
      el("p", { class: "muted" }, "Use ← → to step through the game, or click a move or the graph."));
    return;
  }
  const m = game.moves[ply - 1];
  const cls = m.classification;
  box.replaceChildren(
    el("div", { class: "headline" }, moveLabel(m), " ", el("span", { class: `c-${cls}` }, `— ${LABELS[cls] || cls}`)),
    FLAGGED.includes(cls) ? tagRow(m) : "",
    FLAGGED.includes(cls) ? coachSection(m) : "",
    engineDetails(m),
    askSection(m));
  if (box.dataset.lastPly === String(ply)) box.scrollTop = scroll; // re-render of same move keeps scroll
  box.dataset.lastPly = String(ply);
}

// ---------- chart ----------

function graphValue(winPct) {
  return winPct === null || winPct === undefined ? null : Math.round(winPct * 10) / 10;
}

function renderChart() {
  const labels = ["Start", ...game.moves.map(moveLabel)];
  const values = [graphValue(game.moves[0]?.win_pct_before ?? 50), ...game.moves.map((m) => graphValue(m.win_pct_after))];
  const pointColors = [null, ...game.moves.map((m) => DOT_COLORS[m.classification] || null)];
  const pointRadius = (ctx) => {
    if (ctx.dataIndex === ply) return 5;
    return pointColors[ctx.dataIndex] ? 4 : 0;
  };
  const gridColor = getComputedStyle(document.documentElement).getPropertyValue("--border").trim() || "#ddd";

  chart = new Chart(document.getElementById("chart"), {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: "#888",
        borderWidth: 1.5,
        fill: { target: { value: 50 }, above: "rgba(240,240,240,0.55)", below: "rgba(40,40,40,0.55)" },
        tension: 0.15,
        pointRadius,
        pointHoverRadius: 6,
        pointBackgroundColor: (ctx) => (ctx.dataIndex === ply ? "#2f6f4f" : pointColors[ctx.dataIndex] || "#888"),
        pointBorderWidth: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      onClick: (_, elements) => { if (elements.length) goTo(elements[0].index); },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.dataIndex === 0) return "Start";
              const m = game.moves[ctx.dataIndex - 1];
              return `${formatEval(m.eval_after_cp, m.mate_after)}  ${LABELS[m.classification] || ""}`;
            },
          },
        },
      },
      scales: {
        x: { display: false },
        y: { min: 0, max: 100, ticks: { stepSize: 50, callback: (v) => (v === 50 ? "=" : v === 100 ? "White" : "Black") },
             grid: { color: gridColor } },
      },
    },
  });
}

// ---------- board ----------

let positionToken = 0;

async function setBoard(fen, arrows, badge = null) {
  const token = ++positionToken;
  board.removeArrows();
  board.setMoveBadge(null);
  await board.setPosition(fen, true);
  if (token !== positionToken) return; // user already moved on; skip stale arrows/badge
  for (const [type, uci] of arrows) board.addArrow(type, uci.slice(0, 2), uci.slice(2, 4));
  if (badge) board.setMoveBadge(badge.square, badge.classification);
}

function showPosition() {
  const m = ply > 0 ? game.moves[ply - 1] : null;
  const fen = m ? m.fen_after : (game.moves[0]?.fen_before || FEN.start);
  const arrows = [];
  if (m) {
    arrows.push([ARROW_PLAYED, m.uci]);
    if (m.best_move_uci && m.best_move_uci !== m.uci && m.classification !== "book") arrows.push([ARROW_BEST, m.best_move_uci]);
  }
  // Badge on the square the piece landed on
  setBoard(fen, arrows, m ? { square: m.uci.slice(2, 4), classification: m.classification } : null);
}

// ---------- evaluation bar ----------

function updateEvalBar() {
  const bar = document.getElementById("evalbar");
  const m = ply > 0 ? game.moves[ply - 1] : null;
  const first = game.moves[0];
  const pct = m ? m.win_pct_after : first.win_pct_before; // White's win %
  const cp = m ? m.eval_after_cp : first.eval_before_cp;
  const mate = m ? m.mate_after : first.mate_before;

  let text;
  if (mate === 0) text = pct >= 50 ? "1-0" : "0-1"; // checkmate on the board
  else if (mate !== null && mate !== undefined) text = `M${Math.abs(mate)}`;
  else text = Math.abs((cp || 0) / 100).toFixed(1);

  const whiteBottom = orientation === COLOR.white;
  const whiteAhead = pct >= 50;
  bar.classList.toggle("flipped", !whiteBottom);
  bar.querySelector(".evalbar-fill").style.height = `${Math.max(0, Math.min(100, pct))}%`;
  const label = bar.querySelector(".evalbar-label");
  label.textContent = text;
  // Number sits at the end of the side that's ahead
  label.classList.toggle("on-white", whiteAhead);
  label.classList.toggle("at-bottom", whiteAhead === whiteBottom);
  bar.title = `Evaluation: ${formatEval(cp, mate)} (White win chance ${Math.round(pct)}%)`;
}

// ---------- line playback ("Show line") ----------

function startLine(label, fen, sans, kind) {
  const chess = new Chess(fen);
  const fens = [fen];
  const moves = [];
  for (const san of sans) {
    try {
      const mv = chess.move(san);
      moves.push(mv.from + mv.to);
      fens.push(chess.fen());
    } catch {
      break;
    }
  }
  if (!moves.length) return;
  line = { label, fens, moves, sans: sans.slice(0, moves.length), startFen: fen, idx: 0, arrow: kind === "danger" ? ARROW_PLAYED : ARROW_BEST };
  stepLine(1);
}

function stepLine(delta) {
  line.idx = Math.max(0, Math.min(line.moves.length, line.idx + delta));
  const arrows = line.idx > 0 ? [[line.arrow, line.moves[line.idx - 1]]] : [];
  setBoard(line.fens[line.idx], arrows);
  renderNav();
}

function stopLine() {
  line = null;
  renderNav();
  showPosition();
}

function renderNav() {
  const nav = document.getElementById("nav");
  if (!line) {
    nav.replaceChildren(
      el("button", { title: "Start (↑)", onclick: () => goTo(0) }, "⏮"),
      el("button", { title: "Previous (←)", onclick: () => goTo(ply - 1) }, "◀"),
      el("button", { title: "Next (→)", onclick: () => goTo(ply + 1) }, "▶"),
      el("button", { title: "End (↓)", onclick: () => goTo(game.moves.length) }, "⏭"),
      el("button", { title: "Flip board", onclick: flip }, "⇅"));
    nav.classList.remove("line-mode");
    return;
  }
  const shown = line.idx ? formatLine(line.sans.slice(0, line.idx), line.startFen) : "start of line";
  nav.classList.add("line-mode");
  nav.replaceChildren(
    el("button", { title: "Previous (←)", onclick: () => stepLine(-1) }, "◀"),
    el("button", { title: "Next (→)", onclick: () => stepLine(1) }, "▶"),
    el("span", { class: "line-text", title: formatLine(line.sans, line.startFen) },
      el("strong", {}, `${line.label.replace(/^Show the /, "")}: `), shown),
    el("button", { title: "Back to the game (Esc)", onclick: stopLine }, "✕ Back to game"));
}

function flip() {
  orientation = orientation === COLOR.white ? COLOR.black : COLOR.white;
  board.setOrientation(orientation);
  updateEvalBar();
}

// ---------- navigation ----------

// Scroll only the move list box, never the page (scrollIntoView would move the board too).
function scrollMoveIntoView(node) {
  const list = document.getElementById("movelist");
  const top = node.offsetTop - list.offsetTop;
  const bottom = top + node.offsetHeight;
  if (top < list.scrollTop) list.scrollTop = top - 4;
  else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight + 4;
}

function goTo(newPly) {
  if (line) {
    line = null;
    renderNav();
  }
  ply = Math.max(0, Math.min(game.moves.length, newPly));
  document.querySelectorAll(".mv.current").forEach((n) => n.classList.remove("current"));
  const cur = document.querySelector(`.mv[data-ply="${ply}"]`);
  if (cur) {
    cur.classList.add("current");
    scrollMoveIntoView(cur);
  }
  document.getElementById("move-info").scrollTop = 0;
  renderMoveInfo();
  if (chart) chart.update("none");
  updateEvalBar();
  showPosition();
}

function bindKeys() {
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (line) {
      const lineKeys = { ArrowLeft: () => stepLine(-1), ArrowRight: () => stepLine(1), Escape: stopLine };
      if (lineKeys[e.key]) { e.preventDefault(); lineKeys[e.key](); }
      return;
    }
    const keys = {
      ArrowLeft: () => goTo(ply - 1), ArrowRight: () => goTo(ply + 1),
      ArrowUp: () => goTo(0), ArrowDown: () => goTo(game.moves.length),
      Home: () => goTo(0), End: () => goTo(game.moves.length),
    };
    if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
  });
}

// ---------- loading ----------

async function showStatus() {
  const card = document.getElementById("status-card");
  const st = await (await fetch(`/api/games/${gameId}/status`)).json();
  if (st.status === "done") {
    card.hidden = true;
    return true;
  }
  card.hidden = false;
  if (st.status === "running") {
    card.replaceChildren(el("p", {}, `Analysing… ${st.progress}%`),
      el("div", { class: "progress" }, el("div", { style: `width:${st.progress}%` })));
  } else {
    const msg = st.status === "error" ? `Analysis failed: ${st.error || "unknown error"}` : "This game hasn't been analysed yet.";
    card.replaceChildren(el("p", { class: st.status === "error" ? "error" : "" }, msg),
      el("button", { class: "primary", onclick: async () => {
        await fetch(`/api/games/${gameId}/analyze`, { method: "POST" });
        waitForAnalysis();
      } }, "Analyse now"));
  }
  return false;
}

async function waitForAnalysis() {
  const done = await showStatus();
  if (done) init();
  else {
    const st = await (await fetch(`/api/games/${gameId}/status`)).json();
    if (st.status === "running") setTimeout(waitForAnalysis, 1500);
  }
}

async function init() {
  const res = await fetch(`/api/games/${gameId}`);
  if (!res.ok) {
    document.querySelector("main").replaceChildren(el("p", { class: "error" }, "Game not found."));
    return;
  }
  game = await res.json();
  document.getElementById("title").textContent = `${game.white} vs ${game.black}${game.date ? ` · ${game.date}` : ""}`;
  if (game.analysis_status !== "done" || !game.moves.length) {
    waitForAnalysis();
    return;
  }

  orientation = game.my_color === "black" ? COLOR.black : COLOR.white;
  board = new Chessboard(document.getElementById("board"), {
    position: game.moves[0].fen_before,
    orientation,
    assetsUrl: ASSETS,
    style: { cssClass: "green", borderType: "frame" },
    extensions: [{ class: Arrows }, { class: MoveBadge }],
  });

  renderPlayers();
  renderSummary();
  renderCoachReview();
  renderMoveList();
  renderChart();
  renderNav();
  bindKeys();
  goTo(0);
  if (game.coach_status === "running") pollCoach();
}

init();
