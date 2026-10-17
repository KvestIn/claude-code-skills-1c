# file: command-gen.py
# Генератор команд типовых объектов в расширении по подсистемам БСП из описания JSON:
#   kind "attached" - подключаемая команда (ПодключаемыеКоманды) в форме списка или объекта: перехват
#     ПриОпределенииКомандПодключенныхКОбъекту (и при своем виде - ПриОпределенииВидовПодключаемыхКоманд), серверный
#     обработчик в общем модуле расширения: запрос по выбранным ссылкам, результат - ПараметрыВыполнения.Результат.Текст
#     (библиотека показывает его пользователю);
#   kind "basis" - создание на основании (СозданиеНаОсновании): команда в менеджере источника, заполнение получателя
#     в ОбработкаЗаполнения по сопоставлению реквизитов и табличных частей.
# Имена объектов, реквизитов, табличных частей и видов команд сверяются с выгрузкой; чего нет - отказ со списком.
# Без генератора: команда с незарегистрированным видом "Действие" не появлялась; заполнение было,
# а команды создания на основании не было.
# Запуск: python command-gen.py -SpecPath <spec.json>
import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
SKILLS = Path(__file__).resolve().parents[2]
KINDS = {"Document": ("Documents", "Документ", "Документы", "ДокументСсылка"),
         "Catalog": ("Catalogs", "Справочник", "Справочники", "СправочникСсылка")}
STD = {"Document": {"Номер", "Дата", "Ссылка", "Проведен", "ПометкаУдаления"},
       "Catalog": {"Код", "Наименование", "Ссылка", "Родитель", "Владелец", "ПометкаУдаления"}}
KIND_DIRS = {"catalogs", "documents", "dataprocessors", "reports", "informationregisters", "accumulationregisters",
             "commonmodules", "commonforms", "commontemplates", "enums", "constants", "exchangeplans", "ext",
             "languages", "roles", "subsystems", "definedtypes"}
QUERY_TABLES = {"Документ": "Documents", "Справочник": "Catalogs", "РегистрНакопления": "AccumulationRegisters",
                "РегистрСведений": "InformationRegisters", "Перечисление": "Enums"}
FORMS = {"list": "ФормаСписка", "object": "ФормаОбъекта", "all": ""}
WRITE_MODES = {"НеЗаписывать", "Записывать", "Проводить"}


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def base_dir(spec_path, values):
    """Каталог, от которого считаются относительные пути описания: рабочий каталог, если путь найден в нем, иначе
    каталог файла описания. Раньше пути считались только от каталога описания, и описание, сохраненное в %TEMP%,
    уводило доработку в %TEMP%/src/... мимо выгрузки."""
    rel = [(v, cfg) for v, cfg in values if isinstance(v, str) and v and not Path(v).is_absolute()
           and not re.match(r"^/[a-zA-Z](/|$)", v)]

    def found(root):
        return any((root / v / "Configuration.xml").is_file() if cfg else (root / v).exists() for v, cfg in rel)
    for root in (Path.cwd(), spec_path.parent):
        if found(root):
            return root
    return Path.cwd()


def run_skill(rel, *args):
    p = subprocess.run([sys.executable, str(SKILLS / rel), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        fail(f"{rel}: код {p.returncode}\n{(p.stdout + p.stderr)[-1500:]}")
    return p.stdout


def local(tag):
    return tag.rsplit("}", 1)[-1]


def kid(el, name):
    return next((c for c in el if local(c.tag) == name), None) if el is not None else None


def read_object(config, full):
    kind, _, name = full.partition(".")
    if kind not in KINDS:
        fail(f"Объект '{full}': поддерживаются Document.<Имя> и Catalog.<Имя>.")
    path = Path(config) / KINDS[kind][0] / f"{name}.xml"
    if not path.is_file():
        have = sorted(p.stem for p in (Path(config) / KINDS[kind][0]).glob("*.xml"))
        fail(f"Объекта {full} нет в выгрузке. {KINDS[kind][0]}: {', '.join(have[:100])}"
             f"{' ...' if len(have) > 100 else ''}")
    obj = next(iter(ET.parse(path).getroot()))
    children = kid(obj, "ChildObjects")

    def attrs(el):
        out = {}
        for a in el if el is not None else []:
            if local(a.tag) != "Attribute":
                continue
            out[kid(kid(a, "Properties"), "Name").text] = [
                t.text.strip().split(":")[-1] for t in a.iter() if local(t.tag) == "Type" and t.text and t.text.strip()]
        return out
    tss = {}
    for t in children if children is not None else []:
        if local(t.tag) == "TabularSection":
            tss[kid(kid(t, "Properties"), "Name").text] = attrs(kid(t, "ChildObjects"))
    return {"kind": kind, "name": name, "attributes": attrs(children), "tabular": tss, "std": STD[kind]}


def module_text(config, folder, name, module):
    p = Path(config) / folder / name / "Ext" / module
    return p.read_text(encoding="utf-8-sig", errors="replace") if p.is_file() else ""


def has_method(text, name):
    return re.search(rf"(?im)^\s*(Процедура|Функция)\s+{re.escape(name)}\s*\(", text) is not None


def q(s):
    return '"' + str(s).replace('"', '""') + '"'


def call(head, args):
    if len(args) == 1 and isinstance(args[0], str):
        return [f"{head}({args[0]})"]
    out = [head + "("]
    for i, a in enumerate(args):
        comma = "," if i < len(args) - 1 else ""
        lines = [a] if isinstance(a, str) else a
        for j, ln in enumerate(lines):
            out.append("\t" + ln + (comma if j == len(lines) - 1 else ""))
    out.append(")")
    return out


def stmt(lines, lhs=None):
    lines = list(lines)
    if lhs:
        lines[0] = f"{lhs} = {lines[0]}"
    lines[-1] += ";"
    return lines


def aligned(pairs):
    width = max(len(l) for l, _ in pairs)
    return [f"{l.ljust(width)} = {r};" for l, r in pairs]


def indent(lines, n=1):
    return [("\t" * n + ln) if ln else "" for ln in lines]


def query_lines(config, text, params, ctx):
    for kind_ru, name in re.findall(r"(Документ|Справочник|РегистрНакопления|РегистрСведений|Перечисление)"
                                    r"\.([\wА-Яа-яЁё]+)", text):
        if not (Path(config) / QUERY_TABLES[kind_ru] / f"{name}.xml").is_file():
            fail(f"{ctx}: в запросе таблица {kind_ru}.{name}, такого объекта нет в выгрузке.")
    used = set(re.findall(r"&([\wА-Яа-яЁё]+)", text))
    if used != set(params):
        fail(f"{ctx}: параметры запроса {sorted(used)} не совпадают с {sorted(params)}.")
    text = re.sub(r"^\s*ВЫБРАТЬ(?!\s+РАЗРЕШЕННЫЕ)", "ВЫБРАТЬ РАЗРЕШЕННЫЕ", text.strip(), count=1)
    rows = text.replace('"', '""').split("\n")
    out = ["Запрос = Новый Запрос;", "Запрос.Текст =", '\t"' + rows[0]] + ["\t|" + r for r in rows[1:]]
    out[-1] += '";'
    for p, v in params.items():
        out += stmt(call("Запрос.УстановитьПараметр", [q(p), v]))
    return out


def write_bsl(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(l if l.strip() else "" for l in text.split("\n"))
    path.write_bytes(("﻿" + text.replace("\n", "\r\n")).encode("utf-8"))


def humanize(name):
    name = re.sub(r"^[A-Za-z][A-Za-z0-9]*_", "", name)
    words = re.findall(r"[A-ZА-ЯЁ][a-zа-яё0-9]*|[a-zа-яё0-9]+", name)
    return " ".join([words[0]] + [w.lower() for w in words[1:]]) if words else name


def region(name, procs):
    return [f"#Область {name}", ""] + "\n\n".join("\n".join(p) for p in procs).split("\n") + ["", "#КонецОбласти", ""]


def ensure_empty_or_fail(path, what):
    if path.is_file() and path.read_text(encoding="utf-8-sig").strip():
        fail(f"{what} в расширении уже не пустой ({path}): генератор пишет модуль целиком и чужой код не "
             f"перезаписывает. Используй новое расширение или добавь команду вручную по образцу навыка 1c-command-code.")


class Gen:
    def __init__(self, spec):
        self.spec = spec
        self.config = spec["configPath"]
        self.ext = Path(spec["extension"]["path"])
        self.prefix = ""
        self.files = []

    def borrow(self, obj):
        kind, _, name = obj.partition(".")
        folder = {"Document": "Documents", "Catalog": "Catalogs", "CommonModule": "CommonModules"}[kind]
        if not (self.ext / folder / f"{name}.xml").is_file():
            run_skill("1c-cfe-borrow/scripts/cfe-borrow.py", "-ExtensionPath", self.ext, "-ConfigPath", self.config,
                      "-Object", obj)

    # --- подключаемые команды ---------------------------------------------------------------------------
    def registered_kinds(self):
        kinds = set()
        for p in (Path(self.config) / "CommonModules").glob("*/Ext/Module.bsl"):
            text = p.read_text(encoding="utf-8-sig", errors="replace")
            if "ВидыПодключаемыхКоманд" in text:
                kinds |= set(re.findall(r'Вид\.Имя\s*=\s*"([^"]+)"', text))
        return kinds

    def plan_attached(self, cmds):
        if not (Path(self.config) / "CommonModules" / "ПодключаемыеКомандыПереопределяемый.xml").is_file():
            fail("В выгрузке нет ПодключаемыеКомандыПереопределяемый - конфигурация без подсистемы Подключаемые "
                 "команды БСП, генератор неприменим. Сообщи пользователю.")
        kinds = self.registered_kinds()
        own_kinds, blocks, handlers = {}, [], []
        for c in cmds:
            for k in ("name", "title", "objects"):
                if not c.get(k):
                    fail(f"Команда {c.get('name', '?')}: нет ключа '{k}'.")
            metas = [read_object(self.config, o) for o in c["objects"]]
            if c.get("commandKind"):
                kind_name = c["commandKind"]
                if kind_name not in kinds:
                    fail(f"Команда {c['name']}: вида подключаемых команд '{kind_name}' нет. Зарегистрированные виды: "
                         f"{', '.join(sorted(kinds))}. Не указывай commandKind - генератор зарегистрирует свой вид.")
            else:
                group = c.get("group") or {}
                kind_name = self.prefix + (group.get("name") or "Действия")
                own_kinds[kind_name] = group.get("title") or "Действия"
            forms = c.get("forms", "list")
            if forms not in FORMS:
                fail(f"Команда {c['name']}: forms - list, object или all.")
            mode = c.get("writeMode", "НеЗаписывать")
            if mode not in WRITE_MODES:
                fail(f"Команда {c['name']}: writeMode - {', '.join(sorted(WRITE_MODES))}.")
            handler = self.handler_proc(c, metas)
            handlers.append(handler)
            type_names = ", ".join(f"{KINDS[m['kind']][3]}.{m['name']}" for m in metas)
            cond = " Или ".join(f'Источники.Строки.Найти(Метаданные.{KINDS[m["kind"]][2]}.{m["name"]}, "Метаданные") '
                                f"<> Неопределено" for m in metas)
            pairs = [("Команда.Вид", q(kind_name)), ("Команда.Представление", q(c["title"])),
                     ("Команда.ТипПараметра", f'Новый ОписаниеТипов("{type_names}")'),
                     ("Команда.РежимЗаписи", q(mode)),
                     ("Команда.МножественныйВыбор", "Истина" if c.get("multiple", forms != "object") else "Ложь"),
                     ("Команда.Обработчик", q(f"{self.server_module}.{c['name']}"))]
            if FORMS[forms]:
                pairs.insert(4, ("Команда.ВидимостьВФормах", q(FORMS[forms])))
            blocks.append([f"Если {cond} Тогда"] + indent(["Команда = Команды.Добавить();"] + aligned(pairs))
                          + ["КонецЕсли;"])
        procs = []
        if own_kinds:
            body = []
            for name, title in own_kinds.items():
                body += ["Вид = ВидыПодключаемыхКоманд.Добавить();"] + aligned(
                    [("Вид.Имя", q(name)), ("Вид.Заголовок", q(title)),
                     ("Вид.Отображение", "ОтображениеКнопки.КартинкаИТекст"),
                     ("Вид.ВажностьПриОтображении", "ВажностьПриОтображении.Обычная")]) + [""]
            procs.append(['&После("ПриОпределенииВидовПодключаемыхКоманд")',
                          f"Процедура {self.prefix}ПриОпределенииВидовПодключаемыхКоманд(ВидыПодключаемыхКоманд)", ""]
                         + indent(body[:-1]) + ["", "КонецПроцедуры"])
        body = []
        for b in blocks:
            body += b + [""]
        procs.append(['&После("ПриОпределенииКомандПодключенныхКОбъекту")',
                      f"Процедура {self.prefix}ПриОпределенииКомандПодключенныхКОбъекту(НастройкиФормы, Источники, "
                      f"ПодключенныеОтчетыИОбработки, Команды)", ""] + indent(body[:-1]) + ["", "КонецПроцедуры"])
        return procs, handlers

    def handler_proc(self, c, metas):
        name = c["name"]
        if not re.fullmatch(r"[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*", name):
            fail(f"Команда '{name}': name - идентификатор без пробелов и точек (имя процедуры обработчика).")
        lines = ["Ссылки = Новый Массив;", "Если ТипЗнч(ПараметрКоманды) = Тип(\"Массив\") Тогда",
                 "\tСсылки = ПараметрКоманды;", "Иначе", "\tСсылки.Добавить(ПараметрКоманды);", "КонецЕсли;", ""]
        r = c.get("result")
        if r:
            text = r.get("query", "")
            if not re.search(r"\bКАК\s+Значение\b", text) or "&Ссылки" not in text:
                fail(f"Команда {name}: result.query выбирает одно поле 'КАК Значение' с отбором по &Ссылки "
                     f"(например ГДЕ Таблица.Ссылка В (&Ссылки)).")
            default = r.get("default", 0)
            default = q(default) if isinstance(default, str) else str(default)
            lines += query_lines(self.config, text, {"Ссылки": "Ссылки"}, f"Команда {name}")
            lines += ["", "РезультатЗапроса = Запрос.Выполнить();", "Выборка = РезультатЗапроса.Выбрать();", "",
                      f"Значение = {default};", "Если Выборка.Следующий() И Выборка.Значение <> NULL Тогда",
                      "\tЗначение = Выборка.Значение;", "КонецЕсли;", ""]
            lines += stmt(call("СтрШаблон", [q(r.get("template", "%1")),
                                             f"Формат(Значение, {q(r.get('format', 'ЧДЦ=2; ЧН=0'))})"]),
                          "ПараметрыВыполнения.Результат.Текст")
        elif c.get("body"):
            lines += list(c["body"])
        else:
            fail(f"Команда {name}: укажи result (запрос по &Ссылки и шаблон сообщения) или body (строки кода; "
                 f"доступны Ссылки - Массив выбранных, ПараметрыВыполнения.Результат.Текст - текст для пользователя).")
        head = [f"// Обработчик подключаемой команды \"{c['title']}\".", "//", "// Параметры:",
                "//  ПараметрКоманды - Массив, ЛюбаяСсылка - выбранные объекты.",
                "//  ПараметрыВыполнения - см. ПодключаемыеКоманды.ПараметрыВыполненияКоманды.", "//",
                f"Процедура {name}(ПараметрКоманды, ПараметрыВыполнения) Экспорт", ""]
        return head + indent(lines) + ["", "КонецПроцедуры"]

    # --- создание на основании --------------------------------------------------------------------------
    def plan_basis(self, c):
        src, dst = read_object(self.config, c["source"]), read_object(self.config, c["target"])
        folder_src, folder_dst = KINDS[src["kind"]][0], KINDS[dst["kind"]][0]
        mgr_src = module_text(self.config, folder_src, src["name"], "ManagerModule.bsl")
        mgr_dst = module_text(self.config, folder_dst, dst["name"], "ManagerModule.bsl")
        obj_dst = module_text(self.config, folder_dst, dst["name"], "ObjectModule.bsl")
        dst_ref = f"{KINDS[dst['kind']][2]}.{dst['name']}"
        if re.search(rf"{re.escape(dst_ref)}\.ДобавитьКомандуСоздатьНаОсновании\(", mgr_src):
            fail(f"У {c['source']} команда создания {c['target']} на основании уже есть - добавлять не нужно; "
                 f"проверь заполнение в ОбработкаЗаполнения {c['target']}.")
        if has_method(mgr_dst, "ДобавитьКомандуСоздатьНаОсновании"):
            add = [f"{dst_ref}.ДобавитьКомандуСоздатьНаОсновании(КомандыСозданияНаОсновании);"]
        else:
            add = stmt(call("СозданиеНаОсновании.ДобавитьКомандуСозданияНаОсновании",
                            ["КомандыСозданияНаОсновании", f"Метаданные.{dst_ref}"]))
        listed = any(re.search(rf"Объекты\.Добавить\(Метаданные\.{KINDS[src['kind']][2]}\.{re.escape(src['name'])}\)",
                               p.read_text(encoding="utf-8-sig", errors="replace"))
                     for p in (Path(self.config) / "CommonModules").glob("*/Ext/Module.bsl"))
        mgr = {}
        if has_method(mgr_src, "ДобавитьКомандыСозданияНаОсновании"):
            mgr["head"] = ['&После("ДобавитьКомандыСозданияНаОсновании")',
                           f"Процедура {self.prefix}ДобавитьКомандыСозданияНаОсновании(КомандыСозданияНаОсновании, "
                           f"Параметры)"]
        else:
            mgr["head"] = ["// Добавляет команды создания на основании этого объекта.", "//", "// Параметры:",
                           "//  КомандыСозданияНаОсновании - см. СозданиеНаОснованииПереопределяемый."
                           "ПередДобавлениемКомандСозданияНаОсновании.КомандыСозданияНаОсновании",
                           "//  Параметры - см. СозданиеНаОснованииПереопределяемый."
                           "ПередДобавлениемКомандСозданияНаОсновании.Параметры", "//",
                           "Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСозданияНаОсновании, Параметры) Экспорт"]
        mgr["body"] = add
        mgr["list"] = None if listed else f"Объекты.Добавить(Метаданные.{KINDS[src['kind']][2]}.{src['name']});"

        # заполнение получателя
        header = c.get("header", {})
        for t_attr, s_attr in header.items():
            self.check_attr(dst, t_attr, f"header: реквизит получателя {c['target']}")
            self.check_attr(src, s_attr, f"header: реквизит источника {c['source']}")
            self.check_types(dst["attributes"].get(t_attr), src["attributes"].get(s_attr), f"{t_attr} <- {s_attr}")
        if c.get("basisAttribute"):
            self.check_attr(dst, c["basisAttribute"], "basisAttribute")
            types = dst["attributes"].get(c["basisAttribute"], [])
            if f"{src['kind']}Ref.{src['name']}" not in types:
                fail(f"basisAttribute {c['basisAttribute']}: тип {', '.join(types)} не принимает ссылку {c['source']}.")
        fill = [f'Если ТипЗнч(ДанныеЗаполнения) <> Тип("{KINDS[src["kind"]][3]}.{src["name"]}") Тогда',
                "\tВозврат;", "КонецЕсли;", ""]
        if header:
            fill += stmt(call("ОбщегоНазначения.ЗначенияРеквизитовОбъекта",
                              ["ДанныеЗаполнения", q(", ".join(dict.fromkeys(header.values())))]), "ЗначенияОснования")
            fill += [""] + aligned([(t, f"ЗначенияОснования.{s}") for t, s in header.items()]) + [""]
        if c.get("basisAttribute"):
            fill += [f"{c['basisAttribute']} = ДанныеЗаполнения;", ""]
        for tb in c.get("tables", []):
            ts, tt = tb["source"], tb["target"]
            if ts not in src["tabular"]:
                fail(f"tables: у {c['source']} нет табличной части '{ts}'. Табличные части: "
                     f"{', '.join(sorted(src['tabular'])) or 'нет'}. Копировать нечего - сообщи пользователю.")
            if tt not in dst["tabular"]:
                fail(f"tables: у {c['target']} нет табличной части '{tt}'. Табличные части: "
                     f"{', '.join(sorted(dst['tabular'])) or 'нет'}.")
            cols = tb.get("columns") or fail(f"tables {ts} -> {tt}: нет columns.")
            for t_col, s_col in cols.items():
                if t_col not in dst["tabular"][tt]:
                    fail(f"tables: в {c['target']}.{tt} нет колонки '{t_col}'. Колонки: "
                         f"{', '.join(sorted(dst['tabular'][tt]))}.")
                if s_col not in src["tabular"][ts]:
                    fail(f"tables: в {c['source']}.{ts} нет колонки '{s_col}'. Колонки: "
                         f"{', '.join(sorted(src['tabular'][ts]))}.")
                self.check_types(dst["tabular"][tt][t_col], src["tabular"][ts][s_col], f"{tt}.{t_col} <- {ts}.{s_col}")
            fields = ",\n".join(f"\tСтроки.{s} КАК {t}" for t, s in cols.items())
            text = (f"ВЫБРАТЬ\n{fields}\nИЗ\n\t{KINDS[src['kind']][1]}.{src['name']}.{ts} КАК Строки\nГДЕ\n"
                    f"\tСтроки.Ссылка = &Основание\nУПОРЯДОЧИТЬ ПО\n\tСтроки.НомерСтроки")
            fill += query_lines(self.config, text, {"Основание": "ДанныеЗаполнения"}, f"tables {ts}")
            fill += ["", "РезультатЗапроса = Запрос.Выполнить();", "Выборка = РезультатЗапроса.Выбрать();",
                     f"{tt}.Очистить();", "Пока Выборка.Следующий() Цикл",
                     f"\tЗаполнитьЗначенияСвойств({tt}.Добавить(), Выборка);", "КонецЦикла;", ""]
        while fill and not fill[-1]:
            fill.pop()
        base_sig = re.search(r"(?im)^\s*Процедура\s+ОбработкаЗаполнения\s*\(([^)]*)\)", obj_dst)
        if base_sig:
            # сигнатура перехватчика - как у типового обработчика: в УТ 11.5 он двухпараметровый
            # (ДанныеЗаполнения, СтандартнаяОбработка), перехватчик с тремя параметрами не вызывается
            params = [p.strip() for p in base_sig.group(1).split(",") if p.strip()]
            data_param = re.sub(r"^Знач\s+", "", params[0]) if params else "ДанныеЗаполнения"
            if data_param != "ДанныеЗаполнения":
                fill = [re.sub(r"\bДанныеЗаполнения\b", data_param, ln) for ln in fill]
            head = ['&После("ОбработкаЗаполнения")',
                    f"Процедура {self.prefix}ОбработкаЗаполнения({', '.join(params)})"]
        else:
            head = ["Процедура ОбработкаЗаполнения(ДанныеЗаполнения, ТекстЗаполнения, СтандартнаяОбработка)"]
        dst_attrs = [a for a in header if a not in dst["std"]] + [tb["target"] for tb in c.get("tables", [])]
        if c.get("basisAttribute") and c["basisAttribute"] not in dst["std"]:
            dst_attrs.append(c["basisAttribute"])
        return src, dst, mgr, head + [""] + indent(fill) + ["", "КонецПроцедуры"], list(dict.fromkeys(dst_attrs))

    def check_attr(self, meta, attr, what):
        if attr not in meta["attributes"] and attr not in meta["std"]:
            fail(f"{what}: реквизита '{attr}' нет. Реквизиты: {', '.join(sorted(meta['attributes']))}. Нет данных - "
                 f"не выдумывай реквизит, сообщи пользователю.")

    def check_types(self, t_types, s_types, what):
        t_refs = {t for t in (t_types or []) if "Ref." in t}
        s_refs = {t for t in (s_types or []) if "Ref." in t}
        if t_refs and s_refs and not (t_refs & s_refs):
            fail(f"Сопоставление {what}: типы не совпадают ({', '.join(sorted(t_refs))} и {', '.join(sorted(s_refs))}).")

    # --- запись ---------------------------------------------------------------------------------------------
    def run(self):
        spec = self.spec
        cmds = spec.get("commands") or fail("В описании нет commands.")
        if not (self.ext / "Configuration.xml").is_file():
            if self.ext.is_dir() and any(self.ext.iterdir()):
                found = sorted(str(c.parent) for c in self.ext.glob("*/Configuration.xml"))
                fail(f"extension.path {self.ext} - не каталог расширения и не пустой. Расширения внутри: "
                     f"{', '.join(found) or 'нет'}. Укажи каталог расширения или новый пустой каталог.")
            run_skill("1c-cfe-init/scripts/cfe-init.py", "-Name", spec["extension"]["name"], "-OutputDir", self.ext,
                      "-ConfigPath", self.config, "-NoRole")
        m = re.search(r"<NamePrefix>([^<]*)</NamePrefix>", (self.ext / "Configuration.xml").read_text(encoding="utf-8-sig"))
        self.prefix = m.group(1) if m and m.group(1) else spec["extension"]["name"].split("_")[0] + "_"
        self.server_module = self.prefix + "ПодключаемыеКоманды"
        attached = [c for c in cmds if c.get("kind") == "attached"]
        basis = [c for c in cmds if c.get("kind") == "basis"]
        bad = [c for c in cmds if c.get("kind") not in ("attached", "basis")]
        if bad:
            fail(f"Команда {bad[0].get('name', '?')}: kind - attached (подключаемая команда) или basis (создание на "
                 f"основании).")
        # проверка всего описания до записи
        att_plan = self.plan_attached(attached) if attached else None
        basis_plans = [self.plan_basis(c) for c in basis]
        if att_plan:
            self.write_attached(*att_plan)
        for plan in basis_plans:
            self.write_basis(*plan)
        return self.files

    def write_attached(self, procs, handlers):
        self.borrow("CommonModule.ПодключаемыеКомандыПереопределяемый")
        mod = self.ext / "CommonModules" / "ПодключаемыеКомандыПереопределяемый" / "Ext" / "Module.bsl"
        ensure_empty_or_fail(mod, "Модуль ПодключаемыеКомандыПереопределяемый")
        srv_xml = self.ext / "CommonModules" / f"{self.server_module}.xml"
        srv = self.ext / "CommonModules" / self.server_module / "Ext" / "Module.bsl"
        if not srv_xml.is_file():
            tmp = self.ext / ".command-gen-module.json"
            tmp.write_text(json.dumps({"type": "CommonModule", "name": self.server_module, "server": True},
                                      ensure_ascii=False), encoding="utf-8")
            try:
                run_skill("1c-meta-compile/scripts/meta-compile.py", "-JsonPath", tmp, "-OutputDir", self.ext)
            finally:
                tmp.unlink()
        ensure_empty_or_fail(srv, f"Модуль {self.server_module}")
        write_bsl(mod, "\n".join(region("ПрограммныйИнтерфейс", procs)))
        write_bsl(srv, "\n".join(region("ПрограммныйИнтерфейс", handlers)))
        self.files += [mod, srv]

    def write_basis(self, src, dst, mgr, fill, dst_attrs):
        for meta in (src, dst):
            self.borrow(f"{meta['kind']}.{meta['name']}")
        # реквизиты получателя заимствуются явно: без этого присваивание "Контрагент = ..." в модуле расширения
        # молча заводит локальную переменную, а "Товары.Очистить()" не компилируется
        dst_xml = (self.ext / KINDS[dst["kind"]][0] / f"{dst['name']}.xml").read_text(encoding="utf-8-sig")
        missing = [a for a in dst_attrs if f"<Name>{a}</Name>" not in dst_xml]
        if missing:
            run_skill("1c-cfe-borrow/scripts/cfe-borrow.py", "-ExtensionPath", self.ext, "-ConfigPath", self.config,
                      "-Object", f"{dst['kind']}.{dst['name']}", "-Attributes", ",".join(missing))
        folder_src, folder_dst = KINDS[src["kind"]][0], KINDS[dst["kind"]][0]
        mgr_mod = self.ext / folder_src / src["name"] / "Ext" / "ManagerModule.bsl"
        obj_mod = self.ext / folder_dst / dst["name"] / "Ext" / "ObjectModule.bsl"
        ensure_empty_or_fail(mgr_mod, f"Модуль менеджера {src['name']}")
        ensure_empty_or_fail(obj_mod, f"Модуль объекта {dst['name']}")
        wrap = ["#Если Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение Тогда", ""]
        tail = ["#КонецЕсли", ""]
        mgr_proc = mgr["head"] + [""] + indent(mgr["body"]) + ["", "КонецПроцедуры"]
        write_bsl(mgr_mod, "\n".join(wrap + region("ПрограммныйИнтерфейс", [mgr_proc]) + tail))
        write_bsl(obj_mod, "\n".join(wrap + region("ОбработчикиСобытий", [fill]) + tail))
        self.files += [mgr_mod, obj_mod]
        if mgr["list"]:
            self.borrow("CommonModule.СозданиеНаОснованииПереопределяемый")
            lst = self.ext / "CommonModules" / "СозданиеНаОснованииПереопределяемый" / "Ext" / "Module.bsl"
            ensure_empty_or_fail(lst, "Модуль СозданиеНаОснованииПереопределяемый")
            proc = ['&После("ПриОпределенииОбъектовСКомандамиСозданияНаОсновании")',
                    f"Процедура {self.prefix}ПриОпределенииОбъектовСКомандамиСозданияНаОсновании(Объекты)", "",
                    "\t" + mgr["list"], "", "КонецПроцедуры"]
            write_bsl(lst, "\n".join(region("ПрограммныйИнтерфейс", [proc])))
            self.files.append(lst)


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("-SpecPath", required=True)
    a = ap.parse_args()
    spec_path = Path(a.SpecPath).resolve()
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        fail(f"Описание {spec_path} - не JSON: {e}")
    for k in ("configPath", "extension", "commands"):
        if k not in spec:
            fail(f"В описании нет ключа '{k}'.")

    root = base_dir(spec_path, [(spec.get("configPath"), True), ((spec.get("extension") or {}).get("path"), False)])

    def norm(value):
        m = re.match(r"^/([a-zA-Z])(/.*)?$", value)
        if m and sys.platform == "win32":
            value = f"{m.group(1).upper()}:{m.group(2) or '/'}"
        return str(root / value)
    spec["configPath"] = norm(spec["configPath"])
    spec["extension"]["path"] = norm(spec["extension"]["path"])
    cfg, ext = Path(spec["configPath"]).resolve(), Path(spec["extension"]["path"]).resolve()
    if not (cfg / "Configuration.xml").is_file():
        fail(f"В configPath нет Configuration.xml: {cfg}. Укажи каталог XML-выгрузки конфигурации.")
    if ext == cfg or (cfg in ext.parents and ext.relative_to(cfg).parts[0].lower() in KIND_DIRS):
        fail(f"extension.path {ext} внутри выгрузки конфигурации: расширение клади в отдельный каталог.")
    files = Gen(spec).run()
    print(f"[OK] Команды в расширении {spec['extension']['name']}: "
          + ", ".join(f"{c.get('kind')} {c.get('name') or c.get('target')}" for c in spec["commands"]))
    for f in files:
        print(f"     {f}")
    print("Дальше: проверь расширение (загрузка и синтаксический контроль); код руками не переписывай - меняй "
          "описание и запускай генератор на чистом расширении.")


if __name__ == "__main__":
    main()
