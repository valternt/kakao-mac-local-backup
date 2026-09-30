#!/usr/bin/env python3
"""Back up and restore only KakaoTalk for Mac's local conversation state.

This is an unofficial, same-Mac, best-effort migration tool. It never uploads data.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


BUNDLE_ID = "com.kakao.KakaoTalkMac"
APP = Path("/Applications/KakaoTalk.app")
CONTAINER = Path.home() / "Library/Containers" / BUNDLE_ID
SUPPORT_REL = Path("Data/Library/Application Support") / BUNDLE_ID
PREFS_REL = Path("Data/Library/Preferences")


def fail(message):
    raise SystemExit(f"오류: {message}")


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def require_stopped():
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "KakaoTalk"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        fail("카카오톡을 메뉴에서 완전히 종료한 뒤 다시 실행하세요.")
    if result.returncode != 1:
        fail("카카오톡 실행 여부를 확인할 수 없습니다. 직접 종료한 뒤 다시 시도하세요.")


def uuid_hash():
    result = subprocess.run(
        ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
    if not match:
        fail("이 Mac의 기기 식별자를 확인하지 못했습니다.")
    return hashlib.sha256(match.group(1).encode()).hexdigest()


def app_version():
    info = APP / "Contents/Info.plist"
    if not info.exists():
        fail("/Applications/KakaoTalk.app이 없습니다. 공식 Mac 앱을 설치하세요.")
    with info.open("rb") as f:
        return plistlib.load(f).get("CFBundleShortVersionString", "unknown")


def checksums(root):
    output = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            fail(f"예상하지 못한 심볼릭 링크: {path}")
        if path.is_file():
            h = hashlib.sha256()
            with path.open("rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
            output[path.relative_to(root).as_posix()] = {
                "sha256": h.hexdigest(),
                "bytes": path.stat().st_size,
            }
    return output


def archive_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def backup(archive):
    require_stopped()
    source = CONTAINER / SUPPORT_REL
    if not source.is_dir():
        fail(f"대화 저장소가 없습니다: {source}")
    if not any(
        p.is_file() and re.fullmatch(r"[0-9a-f]{64,128}", p.name)
        for p in source.iterdir()
    ):
        fail("대화 DB 파일을 찾지 못했습니다. 카카오톡 로그인 및 대화 표시를 먼저 확인하세요.")
    prefs = list((CONTAINER / PREFS_REL).glob(f"{BUNDLE_ID}*.plist"))
    if not prefs:
        fail("카카오톡 계정 설정 파일이 없습니다.")
    archive = archive.expanduser().resolve()
    if archive.exists():
        fail("같은 이름의 보관 파일이 이미 있습니다. 다른 이름을 선택하세요.")
    if archive.suffix.lower() != ".zip":
        fail("보관 파일 이름은 .zip으로 끝나야 합니다.")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if str(archive).startswith(str(CONTAINER) + os.sep):
        fail("카카오톡 저장소 안에는 보관할 수 없습니다.")
    same_volume = archive.parent.stat().st_dev == Path.home().stat().st_dev

    with tempfile.TemporaryDirectory(prefix="kakao-stage-") as temp:
        payload = Path(temp) / "kakao-local"
        data = payload / "support"
        data.parent.mkdir(parents=True)
        run("/usr/bin/ditto", str(source), str(data))
        pref_dst = payload / "preferences"
        pref_dst.mkdir()
        for path in prefs:
            run("/usr/bin/ditto", str(path), str(pref_dst / path.name))
        files = checksums(payload)
        manifest = {
            "format": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "app_version": app_version(),
            "device_uuid_sha256": uuid_hash(),
            "files": files,
        }
        (payload / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        run(
            "/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
            str(payload), str(archive),
        )
    run("/usr/bin/unzip", "-tqq", str(archive))
    checksum = archive_hash(archive)
    (archive.parent / (archive.name + ".sha256")).write_text(
        f"{checksum}  {archive.name}\n", encoding="ascii"
    )
    helper = archive.parent / "kakao_mac_local.py"
    if helper.resolve() != Path(__file__).resolve():
        shutil.copy2(Path(__file__).resolve(), helper)
    total = sum(item["bytes"] for item in files.values())
    print(f"보관 완료: {archive}")
    print(f"파일 {len(files)}개, 원본 {total / 1048576:.1f} MiB")
    print(f"무결성 검사값: {archive}.sha256")
    if same_volume:
        print("주의: 보관 파일이 현재 Mac의 홈 폴더와 같은 볼륨에 있습니다.")
    print("포맷 전 외장 저장장치에 ZIP, SHA256 파일, 이 스크립트가 모두 있는지 확인하세요.")


def extract_verified(archive, temp):
    checksum_path = archive.parent / (archive.name + ".sha256")
    if checksum_path.exists():
        expected = checksum_path.read_text(encoding="ascii").split()[0]
        if archive_hash(archive) != expected:
            fail("ZIP 파일의 SHA-256 검사값이 일치하지 않습니다.")
    run("/usr/bin/unzip", "-tqq", str(archive))
    run("/usr/bin/ditto", "-x", "-k", str(archive), str(temp))
    payload = temp / "kakao-local"
    manifest_path = payload / "manifest.json"
    if not manifest_path.is_file():
        fail("이 도구에서 만든 카카오톡 보관 파일이 아닙니다.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        fail("지원하지 않는 보관 형식입니다.")
    found = checksums(payload)
    expected = dict(manifest["files"])
    expected["manifest.json"] = found.get("manifest.json")
    if found != expected:
        fail("보관 내부의 파일 검사값이 일치하지 않습니다.")
    return payload, manifest


def verify(archive):
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        fail(f"보관 파일이 없습니다: {archive}")
    with tempfile.TemporaryDirectory(prefix="kakao-verify-") as temp:
        _, manifest = extract_verified(archive, Path(temp))
    print(f"검증 완료: {archive}")
    print(f"원본 카카오톡 버전: {manifest['app_version']}")
    print(f"파일 수: {len(manifest['files'])}")


def restore(archive):
    require_stopped()
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        fail(f"보관 파일이 없습니다: {archive}")
    if not CONTAINER.is_dir():
        fail("새 카카오톡을 설치해 한 번 실행한 뒤, 로그인하지 말고 종료하세요.")
    with tempfile.TemporaryDirectory(prefix="kakao-restore-") as temp:
        payload, manifest = extract_verified(archive, Path(temp))
        if manifest["device_uuid_sha256"] != uuid_hash():
            fail("백업한 Mac과 현재 Mac의 기기 식별자가 다릅니다. 복원을 중단합니다.")
        if manifest["app_version"] != app_version():
            print(f"참고: 백업 앱 {manifest['app_version']} / 현재 앱 {app_version()} 버전이 다릅니다.")
        target = CONTAINER / SUPPORT_REL
        if target.exists() and any(
            p.is_file() and re.fullmatch(r"[0-9a-f]{64,128}", p.name)
            for p in target.iterdir()
        ):
            fail("새 카카오톡 대화 저장소에 이미 대화 DB가 있습니다. 덮어쓰지 않았습니다.")
        target.parent.mkdir(parents=True, exist_ok=True)
        run("/usr/bin/ditto", str(payload / "support"), str(target))
        pref_target = CONTAINER / PREFS_REL
        pref_target.mkdir(parents=True, exist_ok=True)
        for path in (payload / "preferences").iterdir():
            run("/usr/bin/ditto", str(path), str(pref_target / path.name))
        if checksums(target) != {
            k.removeprefix("support/"): v
            for k, v in manifest["files"].items() if k.startswith("support/")
        }:
            fail("복사 후 대화 파일 검사값이 일치하지 않습니다.")
    print("카카오톡 데이터 복사와 파일 검사 완료.")
    print("Mac을 재시동한 뒤 카카오톡을 열어 기존 계정으로 로그인하고 과거 대화를 확인하세요.")
    print("이 방법은 카카오의 공식 복원 절차가 아니므로 실제 앱 복원 성공은 아직 검증되지 않았습니다.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "verify", "restore"):
        p = sub.add_parser(name)
        p.add_argument("archive", type=Path, help="ZIP 보관 파일 경로")
    args = parser.parse_args()
    try:
        {"backup": backup, "verify": verify, "restore": restore}[args.command](args.archive)
    except (OSError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
