"""Weakness tags for a mistake, worked out from its verified facts (no LLM needed).

The same vocabulary is used by the LLM coach, so fact tags and coach tags can be merged.
"""
from typing import Optional

THEME_TAGS = (
    "hanging_piece", "missed_capture", "missed_fork", "allowed_fork", "missed_pin", "allowed_pin",
    "skewer", "discovered_attack", "back_rank", "missed_mate", "allowed_mate", "mate_threat",
    "trapped_piece", "ignored_threat", "king_safety", "pawn_structure", "passed_pawn",
    "development", "early_queen", "piece_activity", "bad_trade", "endgame_technique",
    "calculation", "overlooked_check", "missed_win", "opening_principles",
)

# Tags about tactics; positional tags are only added when none of these apply,
# because a tactic is almost always the real reason a move was a mistake.
TACTICAL = {
    "hanging_piece", "missed_capture", "missed_fork", "allowed_fork", "missed_pin", "allowed_pin",
    "skewer", "discovered_attack", "back_rank", "missed_mate", "allowed_mate", "mate_threat",
    "trapped_piece", "ignored_threat",
}


def _types(tactics: Optional[list]) -> set[str]:
    return {t["type"] for t in tactics or []}


def _is_back_rank_mate(facts: dict, mover_king: bool) -> bool:
    """Mate delivered on the mated king's back rank (king still on its first rank)."""
    ks = facts.get("king_safety_after_move" if mover_king else "opponent_king_safety") or {}
    square = ks.get("king_square", "")
    return square[-1:] in ("1", "8")


def tags_from_facts(facts: Optional[dict], classification: Optional[str], phase: Optional[str],
                    move_number: int) -> list[str]:
    if not facts:
        return []
    tags: list[str] = []

    def add(tag: str) -> None:
        if tag not in tags:
            tags.append(tag)

    if classification == "missed_win":
        add("missed_win")

    # --- what the opponent can now do (allowed) ---
    reply = facts.get("opponent_best_reply") or {}
    reply_types = _types(reply.get("tactics"))
    refutation = facts.get("refutation_outcome_for_me") or {}
    me = facts.get("mover")

    reply_text = " ".join(t["text"] for t in reply.get("tactics") or [])
    for h in facts.get("newly_hanging_after_move") or []:
        captured_now = f"captures the {me} {h['piece']} on {h['square']}" in reply_text
        if h["value"] >= 3 and (captured_now or refutation.get("material_change", 0) <= -h["value"] + 1):
            add("hanging_piece")
            break

    if refutation.get("ends_in_checkmate") and refutation.get("mated_side") == me:
        add("allowed_mate")
        if _is_back_rank_mate(facts, mover_king=True):
            add("back_rank")
    if "checkmate" in reply_types:
        add("allowed_mate")
    if "fork" in reply_types:
        add("allowed_fork")
    if "pin" in reply_types:
        add("allowed_pin")
    if "skewer" in reply_types:
        add("skewer")
    if reply_types & {"discovered_attack", "discovered_check"}:
        add("discovered_attack")
    if "mate_threat" in reply_types:
        add("mate_threat")
    if "trapped_piece" in reply_types:
        add("trapped_piece")

    if facts.get("threats_ignored"):
        add("ignored_threat")

    # --- what the best move would have done (missed) ---
    best = facts.get("best_move") or {}
    best_types = _types(best.get("tactics"))
    best_outcome = best.get("outcome_for_me") or {}
    if "checkmate" in best_types or (best_outcome.get("ends_in_checkmate") and best_outcome.get("mated_side") != me):
        add("missed_mate")
    if "fork" in best_types:
        add("missed_fork")
    if "pin" in best_types or "skewer" in best_types:
        add("missed_pin")
    if "capture" in best_types and best_outcome.get("material_change", 0) >= 2:
        add("missed_capture")
    if best_types & {"discovered_attack", "discovered_check"}:
        add("discovered_attack")

    # --- king safety ---
    ks = facts.get("king_safety_after_move") or {}
    if phase != "endgame" and (
        ks.get("enemy_pieces_attacking_king_zone", 0) >= 3
        or ("check" in reply_types and len(ks.get("missing_pawn_shield_files", [])) >= 2)
    ):
        add("king_safety")

    # --- habits in the opening (added even with a tactic: they're patterns worth tracking) ---
    dev = facts.get("development") or {}
    if dev.get("early_queen_move"):
        add("early_queen")
    if phase == "opening" and move_number >= 8 and len(dev.get("undeveloped_minor_pieces", [])) >= 2:
        add("development")

    # --- positional reasons, only when there's no tactical explanation ---
    if not set(tags) & TACTICAL:
        mine = (facts.get("pawn_structure_changes") or {}).get(me, {})
        if any(mine.get(kind, {}).get("created") for kind in ("isolated", "doubled", "backward")):
            add("pawn_structure")
        theirs = (facts.get("pawn_structure_changes") or {}).get("black" if me == "white" else "white", {})
        if theirs.get("passed", {}).get("created"):
            add("passed_pawn")
        activity = facts.get("moved_piece_activity") or {}
        if activity.get("on_the_rim") or (
            activity.get("squares_controlled_after", 0) + 3 <= activity.get("squares_controlled_before", 0)
        ):
            add("piece_activity")
        if phase == "endgame":
            add("endgame_technique")

    return tags


def merge_tags(fact_tags: Optional[list], coach_tags: Optional[list]) -> list[str]:
    """Fact tags first (verified), then any extra tags the coach chose."""
    out = list(fact_tags or [])
    for t in coach_tags or []:
        if t not in out:
            out.append(t)
    return out
