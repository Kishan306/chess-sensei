// Shared bits for starting/stopping reviews and showing what they cost.
import { isMasterclass } from "./nav.js";

export function formatUsd(value) {
  if (value === null || value === undefined) return "unknown cost";
  if (value > 0 && value < 0.01) return "< $0.01";
  return `$${value.toFixed(2)}`;
}

/** "$0.42 · 37 calls" for a game, or "" if it hasn't cost anything yet. */
export function spentLabel(game) {
  if (!game.coach_calls) return "";
  const cost = game.coach_cost_usd === null ? "cost unknown" : formatUsd(game.coach_cost_usd);
  return `${cost} · ${game.coach_calls} call${game.coach_calls === 1 ? "" : "s"}`;
}

/** Ask before spending. gameId = one game, null = all games. Returns true to go ahead. */
export async function confirmReview(gameId, { refresh = false, movesInGame = 0 } = {}) {
  const mc = isMasterclass();
  const est = await (await fetch(`/api/coach/estimate${gameId ? `?game_id=${gameId}` : ""}`)).json();
  let calls = est.calls;
  let cost = est.cost_usd;
  if (refresh && gameId) {
    // Regenerating redoes every explanation, so scale the estimate up
    const perCall = cost && calls ? cost / calls : null;
    calls = (mc ? movesInGame : Math.max(calls, 2)) + 1;
    cost = perCall === null ? null : perCall * calls;
  }
  if (!refresh && calls <= est.games) {
    return confirm("Everything is already explained; this only redoes the game summary " +
                   `(${calls} call${calls === 1 ? "" : "s"}, about ${formatUsd(cost)}). Continue?`);
  }
  const what = mc ? "Masterclass review (every move explained in depth)" : "Coach review (mistakes explained)";
  const scope = gameId ? "this game" : `${est.games} game(s)`;
  const money = cost === null || cost === undefined
    ? "The cost can't be estimated for this model."
    : `Estimated cost: about ${formatUsd(cost)} (based on ${est.cost_basis}).`;
  return confirm(`${what} for ${scope}.\n\nAbout ${calls} LLM calls. ${money}\n\nYou can stop it at any time. Continue?`);
}

export async function stopReview(gameId) {
  await fetch(gameId ? `/api/games/${gameId}/coach/stop` : "/api/coach/stop", { method: "POST" });
}

export const isBusy = (status) => ["running", "queued", "stopping"].includes(status);
