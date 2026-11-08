from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    coach_model: str = "claude-sonnet-5-5"
    coach_effort: str = "medium"  # low | medium | high | xhigh | max
    coach_fallbacks: bool = True  # server-side fallback model if a request is declined
    coach_only_my_moves: bool = True  # when the colour is known, only coach the student's mistakes

    stockfish_path: str = "/usr/games/stockfish"
    engine_depth: int = 18
    engine_time: float = 0.5  # seconds per position; whichever of depth/time comes first
    engine_multipv: int = 3
    engine_threads: int = 4
    engine_hash_mb: int = 256

    my_usernames: str = ""
    player_rating: int = 1200
    database_url: str = "sqlite:///./chess_coach.db"

    # Classification thresholds (drop in win %, mover's point of view)
    inaccuracy_threshold: float = 10
    mistake_threshold: float = 20
    blunder_threshold: float = 30

    @property
    def usernames(self) -> list[str]:
        return [u.strip().lower() for u in self.my_usernames.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
