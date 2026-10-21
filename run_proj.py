from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import time
import zipfile


SCRIPT_NAMES = (
    "01_convert_sources.py",
    "02_merge_csv.py",
    "03_clean_deduplicate.py",
    "04_find_anomalies.py",
)


def find_scripts(project_dir: Path) -> Path:
    candidates = (project_dir / "yoyoflot_four_stage", project_dir)
    for folder in candidates:
        if all((folder / filename).is_file() for filename in SCRIPT_NAMES):
            return folder

    print("\nОшибка: не найдены все четыре скрипта.")
    print("Они должны лежать рядом с run_pipeline.py или в папке yoyoflot_four_stage.")
    for folder in candidates:
        missing = [name for name in SCRIPT_NAMES if not (folder / name).is_file()]
        print(f"  {folder}: отсутствуют {', '.join(missing) if missing else 'нет'}")
    raise FileNotFoundError("Невозможно найти четыре этапа обработки")


def ask_archive() -> Path:
    while True:
        try:
            entered = input("Введите полный путь к ZIP-архиву Airlines-All.zip: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            print("\nВвод отменён.")
            raise SystemExit(1) from exc
        if len(entered) >= 2 and entered[0] == entered[-1] and entered[0] in ("'", '"'):
            entered = entered[1:-1]
        entered = entered.strip()
        if not entered:
            print("Путь не введён. Попробуйте ещё раз.\n")
            continue
        archive = Path(entered).expanduser()
        if not archive.is_file():
            print(f"Файл не найден: {archive}\n")
            continue
        if archive.suffix.lower() != ".zip":
            print("Нужен файл с расширением .zip.\n")
            continue
        try:
            if not zipfile.is_zipfile(archive):
                print("Файл не распознан как ZIP-архив.\n")
                continue
        except OSError as exc:
            print(f"Не удалось открыть архив: {exc}\n")
            continue
        return archive.resolve()


def confirm_overwrite(result_dir: Path) -> bool:
    existing = [
        result_dir / "01_sources" / "manifest.json",
        result_dir / "02_merged.csv",
        result_dir / "03_cleaned.csv",
        result_dir / "04_analysis" / "04_passenger_summary.csv",
    ]
    if not any(path.exists() for path in existing):
        return True
    print(f"\nВ папке {result_dir} уже есть результаты прошлого запуска.")
    print("Новый запуск перезапишет файлы этапов 01–04 (старые CSV этапа 01 будут удалены).")
    try:
        answer = input("Продолжить? [д/Н]: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("д", "да", "y", "yes")


def run_step(number: int, title: str, script: Path, args: list[str], cwd: Path) -> None:
    print("\n" + "=" * 72, flush=True)
    print(f"ЭТАП {number}/4: {title}", flush=True)
    print("=" * 72, flush=True)
    started = time.monotonic()
    try:
        subprocess.run([sys.executable, "-u", str(script), *args], cwd=cwd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"\nОШИБКА на этапе {number}: скрипт завершился с кодом {exc.returncode}.", flush=True)
        print("Последующие этапы не запускаются; исправьте ошибку выше и повторите запуск.", flush=True)
        raise SystemExit(exc.returncode or 1) from exc
    except OSError as exc:
        print(f"\nНе удалось запустить этап {number}: {exc}", flush=True)
        raise SystemExit(1) from exc
    duration = time.monotonic() - started
    print(f"\nЭтап {number} завершён успешно ({duration:.1f} сек.).", flush=True)


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Необходим Python 3.10 или новее (на Windows: py -3.12 run_pipeline.py).")

    project_dir = Path(__file__).resolve().parent
    try:
        script_dir = find_scripts(project_dir)
    except FileNotFoundError:
        raise SystemExit(1) from None

    result_dir = project_dir / "result"
    print("Проект «Поймай шпионов» — автоматическая обработка архива")
    print(f"Python: {sys.executable}")
    print(f"Скрипты: {script_dir}")
    print(f"Результаты: {result_dir}\n")
    archive = ask_archive()
    if not confirm_overwrite(result_dir):
        print("Запуск отменён; файлы не изменены.")
        return

    if importlib.util.find_spec("airportsdata") is None:
        print("\nВнимание: не установлен airportsdata. Проверки аэропортов/времени могут быть неполными.")
    if importlib.util.find_spec("tzdata") is None and sys.platform == "win32":
        print("Внимание: не установлен tzdata. На Windows могут отсутствовать данные часовых поясов.")
    if importlib.util.find_spec("unidecode") is None:
        print("Информация: Unidecode отсутствует, четвёртый этап использует встроенную транслитерацию.")
    excel_enabled = importlib.util.find_spec("openpyxl") is not None
    if not excel_enabled:
        print("Информация: openpyxl отсутствует, результаты этапа 04 сохранятся в CSV без XLSX.")

    output_sources = result_dir / "01_sources"
    output_merged = result_dir / "02_merged.csv"
    output_clean = result_dir / "03_cleaned.csv"
    output_analysis = result_dir / "04_analysis"
    result_dir.mkdir(parents=True, exist_ok=True)

    tasks = (
        (1, "Преобразование исходных файлов в CSV", SCRIPT_NAMES[0],
         [str(archive), "--out", str(output_sources), "--overwrite"]),
        (2, "Объединение CSV", SCRIPT_NAMES[1],
         [str(output_sources), "--out", str(output_merged)]),
        (3, "Очистка, проверка личности и удаление дубликатов", SCRIPT_NAMES[2],
         [str(output_merged), "--out", str(output_clean),
          "--rejected", str(result_dir / "03_rejected.csv.gz"),
          "--incomplete", str(result_dir / "03_incomplete_routes.csv.gz"),
          "--report", str(result_dir / "03_clean_report.json")]),
        (4, "Анализ аномалий и рейтинг 0–10", SCRIPT_NAMES[3],
         [str(output_clean), "--out", str(output_analysis), "--top", "300", "--min-score", "6"]
         + (["--excel"] if excel_enabled else [])),
    )

    started = time.monotonic()
    try:
        for number, title, filename, arguments in tasks:
            run_step(number, title, script_dir / filename, arguments, project_dir)
    except KeyboardInterrupt:
        print("\nВыполнение прервано пользователем.")
        raise SystemExit(130) from None

    print("\n" + "=" * 72)
    print(f"ГОТОВО! Все четыре этапа завершились за {time.monotonic() - started:.1f} сек.")
    print(f"Очищенная таблица: {output_clean}")
    print(f"Рейтинг кандидатов: {output_analysis / '04_all_ranked_candidates.csv'}")
    print(f"Топ-300: {output_analysis / ('04_review_candidates.xlsx' if excel_enabled else '04_review_candidates.csv')}")
    print(f"Отчёт: {output_analysis / '04_analysis_report.json'}")
    print("=" * 72)


if __name__ == "__main__":
    main()
