from app.services.pgn_service import detect_my_color, game_fields, parse_pgn_text


def test_splits_multi_game_file_and_rejects_bad_game(fixture_text):
    games, errors = parse_pgn_text(fixture_text("multi.pgn"))
    assert [g.index for g in games] == [1, 2]
    assert games[1].sans == ["f3", "e5", "g4", "Qh4#"]
    assert len(errors) == 1
    assert errors[0].index == 3
    assert "move 2 (White)" in errors[0].message
    assert "Ke3" in errors[0].message


def test_empty_input_is_an_error():
    games, errors = parse_pgn_text("   ")
    assert games == []
    assert errors and errors[0].index == 0


def test_text_without_games():
    games, errors = parse_pgn_text("this is not a pgn")
    assert games == []
    assert errors


def test_detect_my_color_case_insensitive():
    assert detect_my_color({"White": "player123", "Black": "x"}, ["Player123".lower()]) == "white"
    assert detect_my_color({"White": "x", "Black": "PLAYER123"}, ["player123"]) == "black"
    assert detect_my_color({"White": "x", "Black": "y"}, ["player123"]) is None
    assert detect_my_color({"White": "a", "Black": "b"}, []) is None


def test_game_fields_from_headers(fixture_text):
    games, _ = parse_pgn_text(fixture_text("multi.pgn"))
    f = game_fields(games[0], ["player123"])
    assert f["white"] == "Player123"
    assert f["white_elo"] == 1210
    assert f["opening_eco"] == "C50"
    assert f["opening_name"] == "Italian Game"
    assert f["my_color"] == "white"
    assert f["result"] == "1-0"
    assert "1. e4 e5" in f["pgn_raw"]


def test_chess_com_opening_from_eco_url():
    pgn = '[White "a"]\n[Black "b"]\n[ECOUrl "https://www.chess.com/openings/Sicilian-Defense"]\n\n1. e4 c5 *'
    games, _ = parse_pgn_text(pgn)
    assert game_fields(games[0], [])["opening_name"] == "Sicilian Defense"
