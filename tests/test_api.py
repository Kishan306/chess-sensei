import pytest
from fastapi.testclient import TestClient

from app.main import app


def _upload(client, pgn: str) -> int:
    """Upload a game and return its id (the existing one if it was already uploaded)."""
    body = client.post("/api/games", data={"pgn": pgn}).json()
    return body["created"][0] if body["created"] else body["duplicates"][0]["existing_id"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_upload_paste_and_list(client, fixture_text):
    res = client.post("/api/games", data={"pgn": fixture_text("multi.pgn")})
    assert res.status_code == 200
    body = res.json()
    assert len(body["created"]) == 2
    assert body["errors"][0]["game_index"] == 3

    games = client.get("/api/games").json()
    ids = {g["id"] for g in games}
    assert set(body["created"]) <= ids
    colors = {g["id"]: g["my_color"] for g in games}
    assert colors[body["created"][0]] == "white"
    assert colors[body["created"][1]] == "black"


def test_upload_file(client, fixture_text):
    files = {"file": ("game.pgn", fixture_text("opera_game.pgn").encode(), "application/x-chess-pgn")}
    body = client.post("/api/games", files=files).json()
    assert len(body["created"]) == 1 and body["errors"] == []

    game = client.get(f"/api/games/{body['created'][0]}").json()
    assert game["white"] == "Paul Morphy"
    assert game["my_color"] is None
    assert game["analysis_status"] == "pending"
    assert game["moves"] == []


def test_set_my_color(client, fixture_text):
    gid = _upload(client, fixture_text("opera_game.pgn"))
    res = client.patch(f"/api/games/{gid}", json={"my_color": "black"})
    assert res.status_code == 200 and res.json()["my_color"] == "black"
    assert client.patch(f"/api/games/{gid}", json={"my_color": "purple"}).status_code == 422
    assert client.get("/api/games", params={"color": "black"}).json()[0]["id"] == gid
    # Clearing it again
    assert client.patch(f"/api/games/{gid}", json={"my_color": None}).json()["my_color"] is None
    client.patch(f"/api/games/{gid}", json={"my_color": "black"})
    assert client.get("/api/games", params={"color": "black"}).json()[0]["id"] == gid


def test_upload_nothing(client):
    body = client.post("/api/games", data={"pgn": ""}).json()
    assert body["created"] == [] and body["errors"]


def test_status_and_404(client):
    assert client.get("/api/games/99999").status_code == 404
    assert client.get("/api/games/99999/status").status_code == 404
    assert client.post("/api/games/99999/analyze").status_code == 404


def test_pages_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/review/1").status_code == 200
    assert client.get("/static/js/review.js").status_code == 200


class _FakeAnalyzer:
    """Stands in for Stockfish: every position is equal, best move = first legal move."""

    def __init__(self, session, settings=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def analyse(self, board, deep=False):
        mv = sorted(board.legal_moves, key=lambda m: m.uci())[0]
        return [{"uci": [mv.uci()], "san": [board.san(mv)], "cp": 0, "mate": None}]


def test_analysis_end_to_end_with_fake_engine(client, fixture_text, monkeypatch):
    from app.services import engine_service

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _FakeAnalyzer)
    gid = _upload(client, fixture_text("opera_game.pgn"))

    res = client.post(f"/api/games/{gid}/analyze")  # TestClient runs the background task before returning
    assert res.status_code == 202
    status = client.get(f"/api/games/{gid}/status").json()
    assert status == {"status": "done", "progress": 100, "error": None}

    game = client.get(f"/api/games/{gid}").json()
    assert len(game["moves"]) == 33
    assert game["moves"][0]["classification"] == "book"  # 1. e4
    assert game["moves"][-1]["san"] == "Rd8#"
    assert game["moves"][-1]["win_pct_after"] == 100.0

    # Re-analysing replaces moves instead of duplicating them
    client.post(f"/api/games/{gid}/analyze")
    assert len(client.get(f"/api/games/{gid}").json()["moves"]) == 33


class _BlunderAnalyzer(_FakeAnalyzer):
    """Equal everywhere except after 2...Qg5, where White wins the queen."""

    def analyse(self, board, deep=False):
        if board.epd() == "rnb1kbnr/pppp1ppp/8/4p1q1/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -":
            return [{"uci": ["f3g5"], "san": ["Nxg5"], "cp": 900, "mate": None}]
        return super().analyse(board)


class _FakeCoach:
    def __init__(self, settings=None):
        pass

    def __call__(self, system, messages, model, on_usage=None):
        from app.services import coach_service as C

        if model is C.CoachExplanation:
            return C.CoachExplanation(
                headline="The queen was left hanging.", likely_intention="Attack g2.",
                why_it_fails="3. Nxg5 wins it.", opponent_punishment="3. Nxg5",
                better_move="Nc6", why_better="Develops.", alternatives=[],
                principle="Check your pieces are safe.", theme_tags=["hanging_piece"])
        if model is C.GameSummaryOut:
            return C.GameSummaryOut(
                turning_points=[C.TurningPoint(move="2... Qg5", description="Queen lost.")],
                how_it_was_decided="Lost the queen.", strength_shown="Fast development.",
                main_weakness="Piece safety.", practice_next="Blunder-check every move.")
        return C.AskAnswer(answer="Nc6 keeps everything safe.")


def test_coach_endpoints_with_fakes(client, fixture_text, monkeypatch):
    from app.services import coach_service, engine_service

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _BlunderAnalyzer)
    monkeypatch.setattr(coach_service, "AnthropicCoach", _FakeCoach)
    import app.routes.coach as coach_routes
    monkeypatch.setattr(coach_routes, "EngineAnalyzer", _BlunderAnalyzer)

    gid = _upload(client, fixture_text("queen_blunder.pgn"))
    client.patch(f"/api/games/{gid}", json={"my_color": "black"})
    assert client.post(f"/api/games/{gid}/coach").status_code == 409  # not analysed yet

    client.post(f"/api/games/{gid}/analyze")
    game = client.get(f"/api/games/{gid}").json()
    qg5 = next(m for m in game["moves"] if m["san"] == "Qg5")
    assert qg5["classification"] == "blunder"

    assert client.post(f"/api/games/{gid}/coach").status_code == 202
    assert client.get(f"/api/games/{gid}/coach/status").json()["status"] == "done"
    game = client.get(f"/api/games/{gid}").json()
    assert game["notes"][str(qg5["id"])]["headline"] == "The queen was left hanging."
    assert game["summary"]["practice_next"] == "Blunder-check every move."

    note = client.get(f"/api/moves/{qg5['id']}/coach").json()
    assert note["better_move"] == qg5["best_move_san"]  # unverified "Nc6" replaced by the engine move

    res = client.post(f"/api/moves/{qg5['id']}/ask", json={"question": "Why not Nc6?"})
    assert res.status_code == 200
    assert res.json()["checked_moves"][0]["legal"] is True
    assert client.post(f"/api/moves/{qg5['id']}/ask", json={"question": " "}).status_code == 422

    # Re-analysing clears old coaching
    client.post(f"/api/games/{gid}/analyze")
    game = client.get(f"/api/games/{gid}").json()
    assert game["notes"] == {} and game["summary"] is None and game["coach_status"] == "none"


def test_tags_in_game_detail_and_new_pages(client, fixture_text, monkeypatch):
    from app.services import engine_service

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _BlunderAnalyzer)
    gid = _upload(client, fixture_text("queen_blunder.pgn"))
    client.post(f"/api/games/{gid}/analyze")
    qg5 = next(m for m in client.get(f"/api/games/{gid}").json()["moves"] if m["san"] == "Qg5")
    assert "hanging_piece" in qg5["tags"]
    for page in ("/profile", "/puzzles"):
        assert client.get(page).status_code == 200


def test_duplicate_upload_is_skipped(client, fixture_text):
    pgn = fixture_text("scholars_mate.pgn")
    first = _upload(client, pgn)
    body = client.post("/api/games", data={"pgn": pgn}).json()
    assert body["created"] == []
    assert body["duplicates"][0]["existing_id"] == first
    assert "vs" in body["duplicates"][0]["label"]


def test_duplicate_within_one_upload(client):
    game = '[White "Dup A"]\n[Black "Dup B"]\n[Date "2026.01.02"]\n\n1. d4 d5 2. c4 *\n\n'
    body = client.post("/api/games", data={"pgn": game + game}).json()
    assert len(body["created"]) == 1 and len(body["duplicates"]) == 1
    assert body["duplicates"][0]["game_index"] == 2


def test_same_moves_different_links_are_different_games(client):
    base = '[White "Same A"]\n[Black "Same B"]\n[Date "2026.01.03"]\n[Site "https://lichess.org/{id}"]\n\n1. e4 e5 *\n'
    one = client.post("/api/games", data={"pgn": base.format(id="aaaa1111")}).json()
    two = client.post("/api/games", data={"pgn": base.format(id="bbbb2222")}).json()
    assert one["created"] and two["created"]
    # Same link again (even without the other headers matching exactly) is a duplicate
    again = client.post("/api/games", data={"pgn": base.format(id="AAAA1111").replace("Same A", "same a")}).json()
    assert again["duplicates"][0]["existing_id"] == one["created"][0]


class _MasterclassCoach(_FakeCoach):
    calls = 0

    def __call__(self, system, messages, model, on_usage=None):
        from app.services import coach_service as C

        type(self).calls += 1
        assert "titled" in system  # Masterclass persona
        if model is C.MasterclassMistake:
            base = super().__call__(system, messages, C.CoachExplanation).model_dump()
            return C.MasterclassMistake(**base, master_insight="A strong player first asks what is undefended.")
        if model is C.MoveExplanation:
            return C.MoveExplanation(
                headline="A natural developing move.", idea="Develops a piece.", why_it_works="Controls the centre.",
                opponent_options="The opponent can continue developing.", alternatives=[],
                plan_ahead="Castle and connect the rooks.", master_insight="Tempo matters here.",
                principle="Develop before attacking.")
        return super().__call__(system, messages, model)


def test_masterclass_explains_every_move(client, fixture_text, monkeypatch):
    from app.services import coach_service, engine_service

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _BlunderAnalyzer)
    monkeypatch.setattr(coach_service, "AnthropicCoach", _MasterclassCoach)
    gid = _upload(client, fixture_text("queen_blunder.pgn"))
    client.post(f"/api/games/{gid}/analyze")

    assert client.get("/api/settings").json() == {"masterclass": False}
    est_standard = client.get("/api/coach/estimate", params={"game_id": gid}).json()
    assert client.put("/api/settings", json={"masterclass": True}).json() == {"masterclass": True}
    est = client.get("/api/coach/estimate", params={"game_id": gid}).json()
    # 5 moves, but 1. e4 e5 2. Nf3 are book moves with free notes: 2 explanations + summary
    assert est["mode"] == "masterclass" and est["calls"] == 2 + 1
    assert est["calls"] > est_standard["calls"]

    try:
        assert client.post(f"/api/games/{gid}/coach").status_code == 202
        game = client.get(f"/api/games/{gid}").json()
        assert game["coach_status"] == "done" and game["coach_mode"] == "masterclass"
        notes = game["notes"]
        assert len(notes) == 5  # every move, both sides
        kinds = {m["san"]: notes[str(m["id"])]["kind"] for m in game["moves"]}
        assert kinds == {"e4": "brief", "e5": "brief", "Nf3": "brief", "Qg5": "mistake", "Nxg5": "move"}
        assert _MasterclassCoach.calls == 3  # 2 explanations + summary, nothing for book moves
        assert notes[str(game["moves"][3]["id"])]["master_insight"]
        assert game["summary"]["_mode"] == "masterclass"

        # Nothing left to do afterwards
        assert client.get("/api/coach/estimate", params={"game_id": gid}).json()["calls"] == 1

        # Good moves can be explained on demand only in Masterclass
        e4 = game["moves"][0]
        nxg5 = game["moves"][4]
        assert client.get(f"/api/moves/{nxg5['id']}/coach").json()["kind"] == "move"
    finally:
        client.put("/api/settings", json={"masterclass": False})
    assert client.get(f"/api/moves/{e4['id']}/coach", params={"refresh": True}).status_code == 400


def test_coach_all_queues_and_analyses(client, fixture_text, monkeypatch):
    from app.services import coach_service, engine_service

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _BlunderAnalyzer)
    monkeypatch.setattr(coach_service, "AnthropicCoach", _FakeCoach)
    pgn = '[White "Bulk A"]\n[Black "Bulk B"]\n[Date "2026.02.02"]\n\n1. e4 e5 2. Nf3 Qg5 3. Nxg5 *\n'
    gid = _upload(client, pgn)
    assert client.get(f"/api/games/{gid}").json()["analysis_status"] == "pending"

    res = client.post("/api/coach/all", json={"game_ids": [gid]})
    assert res.status_code == 202 and res.json()["games"] == 1
    game = client.get(f"/api/games/{gid}").json()
    assert game["analysis_status"] == "done" and game["coach_status"] == "done"


def test_coach_reports_config_error_when_every_call_fails(client, fixture_text, monkeypatch):
    from app.services import coach_service, engine_service

    class _Broken:
        def __init__(self, settings=None):
            pass

        def __call__(self, *a, **kw):
            raise coach_service.CoachError("The Anthropic API key is missing or invalid.")

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _BlunderAnalyzer)
    monkeypatch.setattr(coach_service, "AnthropicCoach", _Broken)
    pgn = '[White "Err A"]\n[Black "Err B"]\n[Date "2026.03.03"]\n\n1. e4 e5 2. Nf3 Qg5 3. Nxg5 *\n'
    gid = _upload(client, pgn)
    client.post(f"/api/games/{gid}/analyze")
    client.post(f"/api/games/{gid}/coach")
    st = client.get(f"/api/games/{gid}/coach/status").json()
    assert st["status"] == "error" and "API key" in st["error"]


class _MeteredCoach(_MasterclassCoach):
    """Reports 5000 input / 2000 output Sonnet tokens per call."""

    def __call__(self, system, messages, model, on_usage=None):
        if on_usage:
            on_usage("claude-sonnet-5-5", 5000, 2000)
        return super().__call__(system, messages, model)


def test_cost_is_tracked_per_game_and_used_for_estimates(client, fixture_text, monkeypatch):
    from app.services import coach_service, engine_service

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _BlunderAnalyzer)
    monkeypatch.setattr(coach_service, "AnthropicCoach", _MeteredCoach)
    pgn = '[White "Cost A"]\n[Black "Cost B"]\n[Date "2026.04.04"]\n\n1. e4 e5 2. Nf3 Qg5 3. Nxg5 *\n'
    gid = _upload(client, pgn)
    client.post(f"/api/games/{gid}/analyze")
    client.put("/api/settings", json={"masterclass": True})
    try:
        before = client.get("/api/coach/estimate", params={"game_id": gid}).json()
        assert before["calls"] == 3 and before["cost_usd"] is not None
        client.post(f"/api/games/{gid}/coach")
        game = client.get(f"/api/games/{gid}").json()
        per_call = (5000 * 2 + 2000 * 10) / 1e6
        assert game["coach_calls"] == 3
        assert game["coach_cost_usd"] == pytest.approx(3 * per_call)
        # The next estimate is based on what calls actually cost
        other = _upload(client, pgn.replace("Cost A", "Cost C"))
        client.post(f"/api/games/{other}/analyze")
        est = client.get("/api/coach/estimate", params={"game_id": other}).json()
        assert est["cost_basis"] == "your past reviews"
        assert est["cost_usd"] == pytest.approx(round(est["calls"] * per_call, 2))
    finally:
        client.put("/api/settings", json={"masterclass": False})


def test_stop_running_review_keeps_finished_notes(client, fixture_text, monkeypatch):
    from sqlmodel import Session

    from app.config import get_settings
    from app.db import engine as db_engine
    from app.models import Game
    from app.services import coach_service, engine_service

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _FakeAnalyzer)
    monkeypatch.setattr(get_settings(), "coach_concurrency", 1)
    gid = _upload(client, fixture_text("opera_game.pgn"))
    client.post(f"/api/games/{gid}/analyze")

    class StopAfterTwo(_MasterclassCoach):
        n = 0

        def __call__(self, system, messages, model, on_usage=None):
            import time

            time.sleep(0.2)  # real calls take seconds; lets the job notice Stop in time
            type(self).n += 1
            if type(self).n == 2:  # the user clicks Stop while the review is running
                with Session(db_engine) as s:
                    g = s.get(Game, gid)
                    g.coach_status = "stopping"
                    s.add(g)
                    s.commit()
            return super().__call__(system, messages, model)

    monkeypatch.setattr(coach_service, "AnthropicCoach", StopAfterTwo)
    client.put("/api/settings", json={"masterclass": True})
    try:
        client.post(f"/api/games/{gid}/coach")
    finally:
        client.put("/api/settings", json={"masterclass": False})
    game = client.get(f"/api/games/{gid}").json()
    assert game["coach_status"] == "stopped" and "Stopped" in game["coach_error"]
    llm_notes = [n for n in game["notes"].values() if n["kind"] != "brief"]
    # The finished calls are kept. At most one more call was already under way when Stop
    # was seen; nothing after that was started.
    assert StopAfterTwo.n in (2, 3) and len(llm_notes) == StopAfterTwo.n
    assert game["summary"] is None


def test_stop_queued_game_is_skipped(client, fixture_text, monkeypatch):
    from sqlmodel import Session

    from app.db import engine as db_engine
    from app.models import Game
    from app.services import coach_service

    gid = _upload(client, fixture_text("queen_blunder.pgn"))
    with Session(db_engine) as s:
        g = s.get(Game, gid)
        g.coach_status = "queued"
        s.add(g)
        s.commit()
    assert client.post(f"/api/games/{gid}/coach/stop").json()["status"] == "stopped"

    called = []
    monkeypatch.setattr(coach_service, "coach_game", lambda *a, **k: called.append(a))
    coach_service.coach_all([gid], "masterclass")
    assert called == []
    assert client.post("/api/coach/stop").json() == {"stopped": 0}
