import pytest
from fastapi.testclient import TestClient

from app.main import app


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
    gid = client.post("/api/games", data={"pgn": fixture_text("opera_game.pgn")}).json()["created"][0]
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

    def analyse(self, board):
        mv = sorted(board.legal_moves, key=lambda m: m.uci())[0]
        return [{"uci": [mv.uci()], "san": [board.san(mv)], "cp": 0, "mate": None}]


def test_analysis_end_to_end_with_fake_engine(client, fixture_text, monkeypatch):
    from app.services import engine_service

    monkeypatch.setattr(engine_service, "EngineAnalyzer", _FakeAnalyzer)
    gid = client.post("/api/games", data={"pgn": fixture_text("opera_game.pgn")}).json()["created"][0]

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

    def analyse(self, board):
        if board.epd() == "rnb1kbnr/pppp1ppp/8/4p1q1/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -":
            return [{"uci": ["f3g5"], "san": ["Nxg5"], "cp": 900, "mate": None}]
        return super().analyse(board)


class _FakeCoach:
    def __init__(self, settings=None):
        pass

    def __call__(self, system, messages, model):
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

    gid = client.post("/api/games", data={"pgn": fixture_text("queen_blunder.pgn")}).json()["created"][0]
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
