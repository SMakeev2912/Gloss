"""Выпуск новой версии одной командой: release.bat (или python tools/release.py).

  release.bat            1.0.0 → 1.0.1 (исправления)
  release.bat minor      1.0.0 → 1.1.0 (новые возможности)
  release.bat major      1.0.0 → 2.0.0
  release.bat 1.5.0      ровно эта версия

Прогоняет тесты, переносит пункты «Что нового» из раздела «Готовится» в CHANGELOG.md под номер
версии (если раздел пуст, спрашивает их), поднимает VERSION в config.py, коммитит все изменения
с сообщением «Gloss 1.0.1», ставит тег v1.0.1 и отправляет на GitHub. Там GitHub Actions собирает
exe и публикует выпуск с этими пунктами в описании; если установлен gh, скрипт дожидается
публикации. Установленные копии сами узнают о новой версии и покажут, что в ней нового.
"""
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.py"
CHANGELOG = ROOT / "CHANGELOG.md"
sys.path.insert(0, str(ROOT))
import changelog  # noqa: E402


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, text=True, encoding="utf-8", capture_output=capture)
    if result.returncode != 0:
        sys.exit(f"Команда не удалась: {' '.join(args)}\n{(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


def current_version() -> str:
    return re.search(r'^VERSION = "([^"]+)"', CONFIG.read_text(encoding="utf-8"), re.M).group(1)


def parse(version: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"Версия должна быть вида 1.2.3, получено: {version}")
    major, minor, patch = (int(n) for n in version.split("."))
    return major, minor, patch


def next_version(old: str, arg: str) -> str:
    major, minor, patch = parse(old)
    if arg in ("", "patch"):
        return f"{major}.{minor}.{patch + 1}"
    if arg == "minor":
        return f"{major}.{minor + 1}.0"
    if arg == "major":
        return f"{major + 1}.0.0"
    new = arg.lstrip("v")
    if parse(new) <= parse(old):
        sys.exit(f"Новая версия {new} должна быть больше текущей {old}.")
    return new


def ask_points() -> list[str]:
    """Раздел «Готовится» пуст: просим описать изменения прямо здесь."""
    print("\nВ CHANGELOG.md пуст раздел «Готовится». Что нового в этой версии для пользователей?")
    print("Простыми словами, по пункту на строку. Пустая строка завершает ввод.")
    items = []
    while line := input("  • ").strip():
        items.append(line)
    if not items:
        sys.exit("Без описания изменений выпуск не делаю: пользователи увидят его в окне «Обновления».")
    return items


def wait_for_release(tag: str, repo_url: str) -> None:
    """Ждёт, пока GitHub Actions соберёт и опубликует выпуск (если есть gh)."""
    if not shutil.which("gh"):
        print(f"Сборка идёт здесь: {repo_url}/actions")
        return
    print("Жду, пока GitHub соберёт и опубликует выпуск (обычно 2–3 минуты)…")
    import time
    run_id = None
    for _ in range(30):  # запуск появляется не сразу после push
        found = json.loads(run("gh", "run", "list", "--branch", tag, "--limit", "1", "--json", "databaseId",
                               capture=True) or "[]")
        if found:
            run_id = str(found[0]["databaseId"])
            break
        time.sleep(2)
    if run_id is None:
        print(f"Не нашёл запуск сборки, посмотрите здесь: {repo_url}/actions")
        return
    watch = subprocess.run(["gh", "run", "watch", run_id, "--exit-status", "--interval", "10"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if watch.returncode == 0:
        print(f"Выпуск опубликован: {repo_url}/releases/tag/{tag}")
    else:
        print(f"Сборка не удалась, подробности: {repo_url}/actions/runs/{run_id}")
        sys.exit(1)


def main() -> None:
    if len(sys.argv) > 2:
        sys.exit(__doc__)
    old = current_version()
    new = next_version(old, sys.argv[1] if len(sys.argv) == 2 else "")
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD", capture=True)
    if branch != "main":
        sys.exit(f"Выпуск делается из ветки main, а сейчас {branch}.")

    print(f"Выпуск Gloss {new} (было {old})")
    print("Прогоняю тесты…")
    run(sys.executable, "-m", "pytest", "-q", "tests")

    log_text = CHANGELOG.read_text(encoding="utf-8")
    items = changelog.unreleased(log_text) or ask_points()
    print("Что нового для пользователей:\n" + "\n".join(f"  • {item}" for item in items))
    date = datetime.date.today().strftime("%d.%m.%Y")
    CHANGELOG.write_text(changelog.release(log_text, new, date, items), encoding="utf-8")

    text = CONFIG.read_text(encoding="utf-8")
    CONFIG.write_text(text.replace(f'VERSION = "{old}"', f'VERSION = "{new}"', 1), encoding="utf-8")
    run("git", "add", "-A")
    changed = run("git", "diff", "--cached", "--name-only", capture=True).splitlines()
    print("В выпуск входят изменения:", ", ".join(changed))
    run("git", "commit", "-q", "-m", f"Gloss {new}")
    run("git", "tag", f"v{new}")
    print("Отправляю на GitHub…")
    run("git", "push", "-q", "origin", "main", f"v{new}")
    repo_url = re.sub(r"^git@github\.com:", "https://github.com/",
                      run("git", "remote", "get-url", "origin", capture=True)).removesuffix(".git")
    wait_for_release(f"v{new}", repo_url)


def _own_console() -> bool:
    """Окно открыто двойным щелчком: в консоли только cmd и этот Python, без терминала пользователя."""
    import ctypes
    processes = (ctypes.c_uint32 * 4)()
    return ctypes.windll.kernel32.GetConsoleProcessList(processes, 4) <= 2


if __name__ == "__main__":
    try:
        main()
    finally:
        if _own_console():
            print()
            input("Нажмите Enter, чтобы закрыть окно…")
