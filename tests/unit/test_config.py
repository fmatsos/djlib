from pathlib import Path

from djlib.config import DjlibConfig


def test_defaults() -> None:
    cfg = DjlibConfig.defaults()
    assert cfg.music_root == Path('/music')
    assert cfg.data_root == Path('/data')
    assert cfg.database_url == 'sqlite:////data/catalog.sqlite'


def test_toml_overrides_paths(tmp_path: Path) -> None:
    p = tmp_path / 'djlib.toml'
    p.write_text('[paths]\nmusic_root="/srv/music"\ndata_root="/srv/data"\n')
    cfg = DjlibConfig.load(p)
    assert cfg.music_root == Path('/srv/music')
    assert cfg.data_root == Path('/srv/data')
