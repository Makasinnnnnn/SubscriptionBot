"""Package Git project files and local env files for a private server transfer."""

import argparse
import io
import subprocess
import tarfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Archive path outside the repository")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = args.output.resolve()
    if output.is_relative_to(root):
        parser.error("Store the private archive outside the Git repository")
    production_env = root / ".env.production"
    if not production_env.is_file():
        parser.error(".env.production is missing")
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True,
    )
    names = {name.decode("utf-8") for name in result.stdout.split(b"\0") if name}
    names.update(
        name for name in (".env", ".env.production", ".env.journal") if (root / name).is_file()
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(output, "w:gz") as archive:
        for name in sorted(names):
            path = root / name
            if not path.is_file():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError(f"Unexpected link outside archive scope: {name}")
            data = path.read_bytes()
            if name.endswith(".sh"):
                data = data.replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(f"propgate/{name}")
            info.size = len(data)
            info.mode = (
                0o600 if path.name.startswith(".env") else 0o755 if name.endswith(".sh") else 0o644
            )
            archive.addfile(info, io.BytesIO(data))
            count += 1
    with tarfile.open(output, "r:gz") as archive:
        assert "propgate/.env.production" in archive.getnames()
        for member in archive.getmembers():
            assert member.name.startswith("propgate/") and ".." not in Path(member.name).parts
            archive.extractfile(member).read()
    output.chmod(0o600)
    print(f"Private server archive: {output} ({count} files, {output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
