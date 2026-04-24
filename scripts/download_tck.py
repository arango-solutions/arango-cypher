from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

TCK_BASE_API_URL = "https://api.github.com/repos/opencypher/openCypher/contents/tck/features"


def _fetch_json(url: str) -> list[dict]:
    req = Request(
        url,
        headers={
            "User-Agent": "arango-cypher-py-tck-downloader",
            "Accept": "application/vnd.github+json",
        },
    )
    with urlopen(req, timeout=60) as resp:  # noqa: S310 - controlled URL
        raw = resp.read().decode("utf-8")
    out = json.loads(raw)
    if not isinstance(out, list):
        raise RuntimeError(f"Unexpected GitHub API response shape from {url!r}")
    return out


def _download_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "arango-cypher-py-tck-downloader"})
    with urlopen(req, timeout=60) as resp:  # noqa: S310 - controlled URL
        return resp.read().decode("utf-8")


def download_tck_features(*, dest_dir: Path, only_match: str | None = None) -> int:
    """
    Download openCypher TCK feature files to `dest_dir`.

    By default downloads everything under `tck/features/`.
    If `only_match` is set, only downloads feature files whose path contains that substring.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    n = 0

    def walk(api_url: str, local_dir: Path) -> None:
        nonlocal n
        local_dir.mkdir(parents=True, exist_ok=True)
        for item in _fetch_json(api_url):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue

            if item_type == "dir":
                url = item.get("url")
                if isinstance(url, str) and url:
                    walk(url, local_dir / name)
                continue

            if item_type != "file" or not name.endswith(".feature"):
                continue

            path_hint = item.get("path")
            if only_match and isinstance(path_hint, str) and only_match not in path_hint:
                continue

            download_url = item.get("download_url")
            if not isinstance(download_url, str) or not download_url:
                continue

            text = _download_text(download_url)
            (local_dir / name).write_text(text, encoding="utf-8")
            n += 1

    walk(TCK_BASE_API_URL, dest_dir)
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="Download openCypher TCK feature files")
    p.add_argument(
        "--dest",
        default=str(Path(__file__).resolve().parents[1] / "tests" / "tck" / "features"),
        help="Destination directory for downloaded .feature files",
    )
    p.add_argument(
        "--only-match",
        default=os.environ.get("TCK_ONLY_MATCH"),
        help="Only download feature files whose path contains this substring (e.g. 'Match1.feature')",
    )
    args = p.parse_args()

    dest = Path(args.dest).resolve()
    n = download_tck_features(dest_dir=dest, only_match=args.only_match)
    print(f"Downloaded {n} feature files into {dest}")


if __name__ == "__main__":
    main()
