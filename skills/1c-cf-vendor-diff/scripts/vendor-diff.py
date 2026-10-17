#!/usr/bin/env python3
# vendor-diff v1.0 - сравнение конфигурации с конфигурацией поставщика (как "Сравнить с конфигурацией
# поставщика" в Конфигураторе) и подготовка отчета для чтения по частям.
# Source: https://github.com/Desko77/claude-code-skills-1c
#
# Что делает:
#   1. Конфигуратор в пакетном режиме: /CompareCfg -FirstConfigurationType VendorConfiguration (или File -
#      файл .cf поставщика) -SecondConfigurationType MainConfiguration -ReportType Full -ReportFormat txt.
#      Вместо запуска можно передать готовый отчет (-ReportFile, UTF-16 или UTF-8).
#   2. Разбирает дерево отчета: объекты с пометками "изменен" (***), "только в основной" (<---),
#      "только у поставщика" (-->), свойства с отличиями, строки кода с диапазонами.
#   3. Для строк кода, которые есть только в основной конфигурации, находит по выгрузке (-ConfigPath) метод,
#      в котором они стоят, или новый метод, который они образуют.
#   4. Пишет в -OutDir: summary.md (сводка), objects/NNN-<объект>.md (по файлу на объект верхнего уровня),
#      changes.json (то же для программ), report-full.txt (отчет платформы в UTF-8).
import argparse
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

# --- Вердикт платформы (общий блок, версия 1) ---
PLATFORM_CLEAN_PHRASES = ("ошибок не обнаружено", "ошибки не обнаружены", "предупреждений не обнаружено",
                          "ошибок: 0", "предупреждений: 0", "errors were not found", "0 errors")
PLATFORM_FATAL_PHRASES = ("неверное свойство объекта метаданных", "не входит в состав объекта метаданных",
                          "неизвестное имя типа", "неизвестный объект метаданных", "неверное значение перечисления",
                          "не может быть приведен к типу", "необходима версия платформы не меньше",
                          "не найден метод", "не может быть применен")


def hide_platform_secret(text):
    if not text:
        return text
    keys = r'(?:^|(?<=\s))(/ConfigurationRepositoryP|/UC|/P)'
    masked = re.sub(keys + r'"[^"]*"', r'\g<1>"***"', text)
    return re.sub(keys + r'([^\s"]\S*)', r'\g<1>***', masked)


def platform_log_problems(log_text):
    problems = []
    for line in (log_text or "").splitlines():
        lower = line.strip().lower()
        if not lower or any(p in lower for p in PLATFORM_CLEAN_PHRASES):
            continue
        if any(p in lower for p in PLATFORM_FATAL_PHRASES):
            problems.append(line.strip())
    return problems


def platform_result_code(result_file):
    try:
        with open(result_file, "r", encoding="utf-8-sig") as f:
            return int(f.read().strip())
    except Exception:
        return None
# --- Конец общего блока вердикта платформы ---


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def resolve_v8path(v8path):
    if not v8path:
        candidates = sorted(glob.glob(r"C:\Program Files\1cv8\*\bin\1cv8.exe"))
        if not candidates:
            fail("1cv8.exe не найден. Укажи -V8Path (каталог bin платформы).")
        return candidates[-1]
    if os.path.isdir(v8path):
        v8path = os.path.join(v8path, "1cv8.exe")
    if not os.path.isfile(v8path):
        fail(f"1cv8.exe не найден: {v8path}")
    return v8path


def config_name(config_path):
    """Имя конфигурации из Configuration.xml выгрузки - у конфигурации на поддержке оно совпадает с именем
    конфигурации поставщика."""
    path = os.path.join(config_path, "Configuration.xml")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        m = re.search(r"<Properties>\s*.*?<Name>([^<]+)</Name>", f.read(20000), re.S)
    return m.group(1) if m else ""


def run_compare(args, out_report):
    v8 = resolve_v8path(args.V8Path)
    tmp = os.path.join(tempfile.gettempdir(), f"vendor_diff_{random.randint(0, 999999)}")
    os.makedirs(tmp, exist_ok=True)
    log_file, result_file = os.path.join(tmp, "log.txt"), os.path.join(tmp, "result.txt")
    a = ["DESIGNER"]
    a += ["/S", f"{args.InfoBaseServer}/{args.InfoBaseRef}"] if args.InfoBaseServer else ["/F", args.InfoBasePath]
    if args.UserName:
        a.append(f"/N{args.UserName}")
    if args.Password:
        a.append(f"/P{args.Password}")
    a += ["/DisableStartupDialogs", "/CompareCfg"]
    if args.VendorCf:
        a += ["-FirstConfigurationType", "File", "-FirstFile", args.VendorCf]
    else:
        a += ["-FirstConfigurationType", "VendorConfiguration", "-FirstName", args.VendorName]
    a += ["-SecondConfigurationType", "MainConfiguration", "-ReportType", "Full", "-ReportFormat", "txt",
          "-ReportFile", out_report, "/Out", log_file, "/DumpResult", result_file]
    print("Running: 1cv8.exe " + hide_platform_secret(" ".join(a)))
    proc = subprocess.run([v8] + a, capture_output=True, text=True)
    log = ""
    if os.path.isfile(log_file):
        with open(log_file, encoding="utf-8-sig", errors="replace") as f:
            log = f.read().strip()
    code = platform_result_code(result_file)
    shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0 or (code not in (None, 0)) or not os.path.isfile(out_report):
        hint = ""
        if "недоступна" in log.lower():
            hint = ("\nКонфигурации поставщика в базе нет (база собрана не из .cf поставщика или снята с поддержки). "
                    "Сравни с файлом поставщика той же версии: -VendorCf <типовая>.cf.")
        elif "имя конфигурации поставщика" in log.lower():
            hint = "\nУкажи -VendorName: имя конфигурации поставщика (Name из Configuration.xml типовой)."
        fail(f"Сравнение не выполнено (код {proc.returncode}). Журнал платформы:\n{log}{hint}")
    problems = platform_log_problems(log)
    if problems:
        fail("Платформа сообщила об ошибках:\n" + "\n".join(problems))
    return log


def read_report(path):
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-16-le")


# --- Разбор отчета --------------------------------------------------------------------------------------------

MARKERS = (("***", "изменен"), ("<--", "только в основной"), ("-->", "только у поставщика"), ("^", "порядок изменен"))
LEGEND = re.compile(r"^(\*\*\*|-->|<--|\^)-\s")


class Node:
    def __init__(self, depth, marker, title):
        self.depth, self.marker, self.title = depth, marker, title
        self.children, self.details = [], []


def parse_report(text):
    root = Node(-1, "", "")
    stack = [root]
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip("\t"))
        body = line.strip()
        if LEGEND.match(body):
            continue
        if body.startswith("- "):
            rest = body[2:].strip()
            marker = ""
            for m, _ in MARKERS:
                if rest.startswith(m):
                    marker, rest = m, rest[len(m):].strip()
                    break
            node = Node(depth, marker, rest)
            while len(stack) > 1 and stack[-1].depth >= depth:
                stack.pop()
            stack[-1].children.append(node)
            stack.append(node)
        else:
            stack[-1].details.append(body)
    return root


RU_FOLDERS = {
    "Справочник": "Catalogs", "Документ": "Documents", "ОбщийМодуль": "CommonModules", "Обработка": "DataProcessors",
    "Отчет": "Reports", "РегистрСведений": "InformationRegisters", "РегистрНакопления": "AccumulationRegisters",
    "РегистрБухгалтерии": "AccountingRegisters", "РегистрРасчета": "CalculationRegisters", "Перечисление": "Enums",
    "ПланВидовХарактеристик": "ChartsOfCharacteristicTypes", "ПланСчетов": "ChartsOfAccounts",
    "ПланВидовРасчета": "ChartsOfCalculationTypes", "ПланОбмена": "ExchangePlans", "ЖурналДокументов": "DocumentJournals",
    "Константа": "Constants", "ОбщаяФорма": "CommonForms", "ОбщаяКоманда": "CommonCommands",
    "БизнесПроцесс": "BusinessProcesses", "Задача": "Tasks", "ВебСервис": "WebServices", "HTTPСервис": "HTTPServices",
    "Роль": "Roles", "Подсистема": "Subsystems", "ОпределяемыйТип": "DefinedTypes", "ОбщийМакет": "CommonTemplates",
    "ПодпискаНаСобытие": "EventSubscriptions", "РегламентноеЗадание": "ScheduledJobs",
    "ФункциональнаяОпция": "FunctionalOptions", "ОбщийРеквизит": "CommonAttributes", "ПараметрСеанса": "SessionParameters",
    "КритерийОтбора": "FilterCriteria", "ХранилищеНастроек": "SettingsStorages", "ГруппаКоманд": "CommandGroups",
    "ОбщаяКартинка": "CommonPictures", "ПакетXDTO": "XDTOPackages", "Последовательность": "Sequences",
    "НумераторДокументов": "DocumentNumerators", "ЭлементСтиля": "StyleItems", "Стиль": "Styles", "Язык": "Languages",
    "ВнешнийИсточникДанных": "ExternalDataSources", "WSСсылка": "WSReferences",
}
MODULE_FILES = {"модуль объекта": "ObjectModule.bsl", "модуль менеджера": "ManagerModule.bsl",
                "модуль набора записей": "RecordSetModule.bsl", "модуль менеджера значения": "ValueManagerModule.bsl",
                "модуль команды": "CommandModule.bsl", "модуль": "Module.bsl"}
SUB_FOLDERS = {"Форма": "Forms", "Команда": "Commands", "Макет": "Templates"}
RANGE = re.compile(r"^(Объект присутствует только в основной конфигурации|Объект присутствует только в "
                   r"конфигурации поставщика|Объект изменен)[^:]*:\s*(\d+)\s*-\s*(\d+)")


def module_path(config_path, object_title, prop):
    """Файл модуля в выгрузке по имени объекта отчета ("ОбщийМодуль.X", "Справочник.X.Форма.Y") и свойству."""
    if not config_path:
        return None
    parts = object_title.split(".")
    folder = RU_FOLDERS.get(parts[0])
    if not folder or len(parts) < 2:
        return None
    base = os.path.join(config_path, folder, parts[1])
    key = prop.split(" - ")[0].strip().lower()
    if len(parts) >= 4 and parts[2] in ("Форма",):
        p = os.path.join(base, "Forms", parts[3], "Ext", "Form", "Module.bsl")
    elif len(parts) >= 4 and parts[2] == "Команда":
        p = os.path.join(base, "Commands", parts[3], "Ext", "CommandModule.bsl")
    elif parts[0] == "ОбщаяФорма":
        p = os.path.join(base, "Ext", "Form", "Module.bsl")
    elif parts[0] == "ОбщаяКоманда":
        p = os.path.join(base, "Ext", "CommandModule.bsl")
    else:
        fname = MODULE_FILES.get(key)
        if not fname:
            return None
        p = os.path.join(base, "Ext", fname)
    return p if os.path.isfile(p) else None


def methods_of(path):
    """[(начало, конец, имя, вид)] методов модуля, строки с 1."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    out, cur = [], None
    for i, line in enumerate(lines, 1):
        m = re.match(r"^\s*(Процедура|Функция)\s+([\wА-Яа-яЁё]+)\s*\(", line, re.I)
        if m:
            cur = [i, None, m.group(2), m.group(1)]
        elif cur and re.match(r"^\s*Конец(Процедуры|Функции)\b", line, re.I):
            cur[1] = i
            out.append(tuple(cur))
            cur = None
    return out


def locate(methods, start, end):
    # новый метод - диапазон целиком накрывает объявление и конец (проверяется первым: диапазон, совпадающий с
    # границами метода, иначе попал бы в "в методе")
    new = [m for m in methods if start <= m[0] and m[1] <= end]
    if new:
        return "новые методы: " + ", ".join(f"{m[3]} {m[2]}" for m in new)
    inside = [m for m in methods if m[0] <= start and end <= m[1]]
    if inside:
        return f"в методе {inside[0][2]} (строки {inside[0][0]}-{inside[0][1]})"
    touched = [m[2] for m in methods if not (m[1] < start or m[0] > end)]
    if touched:
        return "затрагивает методы: " + ", ".join(touched)
    return "вне методов (область переменных или комментарии)"


# --- Сборка результата ----------------------------------------------------------------------------------------

def walk(node, path, sink):
    for ch in node.children:
        sink.append((path + [ch], ch))
        walk(ch, path + [ch], sink)


def part_label(top_title, path, title):
    """Подпись добавленной или удаленной части: "Реквизит.X" или "форма ФормаЭлемента: элемент X"."""
    if title.startswith(top_title + "."):
        return title[len(top_title) + 1:]
    form = next((p.title for p in reversed(path[:-1]) if ".Форма." in p.title or p.title.startswith("ОбщаяФорма.")), "")
    return f"форма {form.split('.')[-1]}: элемент {title}" if form else title


def describe_object(top, config_path):
    """Строки описания объекта верхнего уровня и признаки для сводки."""
    md, info = [], {"code": False, "props": set(), "values_only": True, "added": [], "removed": []}
    flat = []
    walk(top, [top], flat)
    for path, n in flat:
        depth = len(path) - 1
        indent = "  " * (depth - 1)
        mark = dict(MARKERS).get(n.marker, "")
        md.append(f"{indent}- {n.title}" + (f" [{mark}]" if mark else ""))
        if n.marker in ("<--", "-->"):
            info["added" if n.marker == "<--" else "removed"].append(part_label(top.title, path, n.title))
        owner = next((p.title for p in reversed(path[:-1]) if "." in p.title), top.title)
        prop = n.title
        if " - " in prop:
            info["props"].add(prop.split(" - ")[0].strip())
        # лист без пометки, где не сказано "различаются значения", - изменение состава или свойства, а не
        # только значений (пересохраненные макеты и настройки дают именно "различаются значения")
        leaf_text = (prop + " " + " ".join(n.details)).lower()
        if not n.children and not n.marker and "различаются значения" not in leaf_text:
            info["values_only"] = False
        if n.marker in ("<--", "-->"):
            info["values_only"] = False
        if n.details and whitespace_only(n.details):
            md[-1] += " [отличие только в пробелах и переносах строк - не доработка]"
            md.extend(f"{indent}  {d}" for d in n.details)
            continue
        mpath = module_path(config_path, owner, prop) if prop.lower().startswith("модуль") else None
        methods = methods_of(mpath) if mpath else None
        for d in n.details:
            m = RANGE.match(d)
            if m:
                info["code"] = True
                info["values_only"] = False
                where = ""
                if methods is not None and "основной" in m.group(1):
                    where = " - " + locate(methods, int(m.group(2)), int(m.group(3)))
                md.append(f"{indent}  {d}{where}")
            else:
                md.append(f"{indent}  {d}")
    return md, info


def whitespace_only(details):
    """Отличие значения только в пробелах и переносах строк: блоки "Конфигурация поставщика" / "Основная
    конфигурация" или строки "<" / ">" совпадают без пробельных символов. Так выглядят многострочные подсказки и
    заголовки после повторной загрузки конфигурации из XML."""
    def norm(lines):
        return re.sub(r"\s+", "", "".join(lines))
    if "Конфигурация поставщика" in details and "Основная конфигурация" in details:
        i, j = details.index("Конфигурация поставщика"), details.index("Основная конфигурация")
        a, b = (details[i + 1:j], details[j + 1:]) if i < j else (details[j + 1:i], details[i + 1:])
        return bool(a) and norm(a) == norm(b)
    lt = [k for k, d in enumerate(details) if d.startswith("<")]
    gt = [k for k, d in enumerate(details) if d.startswith(">")]
    if len(lt) == 1 and len(gt) == 1 and lt[0] < gt[0]:
        a = [details[lt[0]][1:]] + details[lt[0] + 1:gt[0]]
        b = [details[gt[0]][1:]] + details[gt[0] + 1:]
        return norm(a) == norm(b)
    return False


def safe_name(title):
    return re.sub(r'[\\/:*?"<>|]', "_", title)[:120]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("-InfoBasePath", default="")
    ap.add_argument("-InfoBaseServer", default="")
    ap.add_argument("-InfoBaseRef", default="")
    ap.add_argument("-UserName", default="")
    ap.add_argument("-Password", default="")
    ap.add_argument("-V8Path", default="")
    ap.add_argument("-VendorName", default="")
    ap.add_argument("-VendorCf", default="")
    ap.add_argument("-ConfigPath", default="")
    ap.add_argument("-ReportFile", default="")
    ap.add_argument("-OutDir", required=True)
    a = ap.parse_args()

    os.makedirs(a.OutDir, exist_ok=True)
    full_txt = os.path.join(a.OutDir, "report-full.txt")
    if a.ReportFile:
        if not os.path.isfile(a.ReportFile):
            fail(f"Нет файла отчета: {a.ReportFile}")
        text = read_report(a.ReportFile)
    else:
        if not a.InfoBasePath and not (a.InfoBaseServer and a.InfoBaseRef):
            fail("Укажи базу: -InfoBasePath <каталог файловой базы> или -InfoBaseServer и -InfoBaseRef "
                 "(или готовый отчет -ReportFile).")
        if not a.VendorCf and not a.VendorName:
            a.VendorName = config_name(a.ConfigPath) if a.ConfigPath else ""
            if not a.VendorName:
                fail("Не известно имя конфигурации поставщика: укажи -VendorName или -ConfigPath (выгрузку, "
                     "имя возьмется из Configuration.xml).")
        raw = os.path.join(a.OutDir, "report-utf16.txt")
        run_compare(a, raw)
        text = read_report(raw)
        os.remove(raw)
    with open(full_txt, "w", encoding="utf-8") as f:
        f.write(text)

    root = parse_report(text)
    cfg = next((n for n in root.children if n.title.startswith("Конфигурация.")), None)
    tops = cfg.children if cfg else []
    objdir = os.path.join(a.OutDir, "objects")
    if os.path.isdir(objdir):
        shutil.rmtree(objdir)
    os.makedirs(objdir)
    groups = {"added": [], "removed": [], "code": [], "struct": [], "values": []}
    records = []
    for i, top in enumerate(tops, 1):
        md, info = describe_object(top, a.ConfigPath)
        fname = f"{i:03d}-{safe_name(top.title)}.md"
        status = dict(MARKERS).get(top.marker, "изменен")
        with open(os.path.join(objdir, fname), "w", encoding="utf-8") as f:
            f.write(f"# {top.title} - {status}\n\n" + "\n".join(md) + "\n")
        kind = ("added" if top.marker == "<--" else "removed" if top.marker == "-->" else
                "code" if info["code"] else "values" if info["values_only"] else "struct")
        groups[kind].append((top.title, fname, info))
        records.append({"object": top.title, "status": status, "group": kind, "file": f"objects/{fname}",
                        "code": info["code"], "added": info["added"], "removed": info["removed"],
                        "properties": sorted(info["props"])})
    with open(os.path.join(a.OutDir, "changes.json"), "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)

    titles = {"added": "Новые объекты (только в основной конфигурации)",
              "removed": "Удаленные объекты (есть только у поставщика)",
              "code": "Изменен код", "struct": "Изменены свойства, состав или формы (без кода)",
              "values": "Отличаются только значения (макеты, настройки) - проверить: часто это пересохранение, а "
                        "не доработка"}
    out = [f"# Отличия от конфигурации поставщика", "",
           f"Объектов верхнего уровня с отличиями: {len(tops)}. Подробности по каждому - файл в objects/.", ""]
    for k in ("added", "removed", "code", "struct", "values"):
        if not groups[k]:
            continue
        out += [f"## {titles[k]}: {len(groups[k])}", ""]
        for title, fname, info in groups[k]:
            extra = []
            if info["added"] and k != "added":
                extra.append("добавлено: " + ", ".join(info["added"][:8]))
            if info["removed"] and k != "removed":
                extra.append("удалено: " + ", ".join(info["removed"][:8]))
            out.append(f"- {title} - `objects/{fname}`" + (" (" + "; ".join(extra) + ")" if extra else ""))
        out.append("")
    if not tops:
        out.append("Отличий от конфигурации поставщика нет.")
    with open(os.path.join(a.OutDir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"[OK] Отличий: {len(tops)} объектов (новых {len(groups['added'])}, удаленных {len(groups['removed'])}, "
          f"с кодом {len(groups['code'])}, без кода {len(groups['struct'])}, только значения {len(groups['values'])})")
    print(f"     {os.path.join(a.OutDir, 'summary.md')}")
    print("Дальше: прочитай summary.md, затем файлы objects/ по каждому объекту; описывай только то, что в них есть.")


if __name__ == "__main__":
    main()
