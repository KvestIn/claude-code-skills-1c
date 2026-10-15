# file: load-gen.py
# Генератор внешней обработки загрузки данных из файла по описанию JSON. Модель описывает, ЧТО загрузить (формат и
# колонки файла, параметры формы, приемник: справочник, регистр сведений или документы, сопоставление колонок с
# реквизитами и поиск ссылок), генератор сверяет описание с выгрузкой конфигурации и собирает обработку навыками
# набора: каркас (1c-epf-scaffold), форму (1c-epf-add-form, 1c-form-compile) и модуль формы - выбор файла по образцу
# навыка 1c-file-upload (ФайловаяСистемаКлиент.ЗагрузитьФайл, временное хранилище), чтение CSV/Excel/JSON на сервере,
# поиск ссылок одним запросом на файл, запись, счетчики и таблица ошибок на форме.
# Реквизит, измерение или колонка, которых нет в выгрузке, - отказ со списком имеющихся: недостающие данные не
# выдумываются (замер 15.10.2026: ни одно из решений задач загрузки не было полностью рабочим; провалы - передача
# файла, сопоставление с метаданными и умолчание об ограничениях).
# Запуск: python load-gen.py -SpecPath <spec.json>
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
FOLDERS = {"Catalog": "Catalogs", "Document": "Documents", "InformationRegister": "InformationRegisters"}
MANAGERS = {"Catalog": "Справочники", "Document": "Документы", "InformationRegister": "РегистрыСведений"}
QUERY_TABLES = {"Catalog": "Справочник", "Document": "Документ"}
STD = {"Catalog": {"Наименование": "string", "Код": "string", "Родитель": "ref", "Владелец": "ref",
                   "ПометкаУдаления": "boolean"},
       "Document": {"Дата": "date", "Номер": "string", "ПометкаУдаления": "boolean"},
       "InformationRegister": {}}
FORMATS = {"csv": ("csv", "Файлы CSV (*.csv)|*.csv|Все файлы (*.*)|*.*"),
           "excel": ("xlsx", "Файлы Excel (*.xlsx;*.xls)|*.xlsx;*.xls|Все файлы (*.*)|*.*"),
           "json": ("json", "Файлы JSON (*.json)|*.json|Все файлы (*.*)|*.*")}


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def run_skill(rel, *args):
    p = subprocess.run([sys.executable, str(SKILLS / rel), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        fail(f"{rel}: код {p.returncode}\n{(p.stdout + p.stderr)[-1500:]}")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def kid(el, name):
    return next((c for c in el if local(c.tag) == name), None) if el is not None else None


def kind_of(types):
    """Вид значения по типам реквизита: ref, number, date, boolean, string."""
    t = types[0] if types else "string"
    if "Ref." in t:
        return "ref"
    if "decimal" in t:
        return "number"
    if "dateTime" in t or t == "date":
        return "date"
    if "boolean" in t:
        return "boolean"
    return "string"


def read_object(config, full):
    kind, _, name = full.partition(".")
    if kind not in FOLDERS:
        fail(f"'{full}': поддерживаются Catalog.<Имя>, Document.<Имя>, InformationRegister.<Имя>.")
    path = Path(config) / FOLDERS[kind] / f"{name}.xml"
    if not path.is_file():
        fail(f"Объекта {full} нет в выгрузке ({path}). Сверь имя по выгрузке: синоним и имя различаются.")
    obj = next(iter(ET.parse(path).getroot()))
    props, children = kid(obj, "Properties"), kid(obj, "ChildObjects")

    def fields(el, tags):
        out = {}
        for a in el if el is not None else []:
            if local(a.tag) in tags:
                n = kid(kid(a, "Properties"), "Name")
                # внешний <Type> содержит только пробелы и вложенные <v8:Type> - берется непустой текст
                out[n.text] = [t.text.strip().split(":")[-1] for t in a.iter()
                               if local(t.tag) == "Type" and t.text and t.text.strip()]
        return out
    res = {"kind": kind, "name": name, "fields": dict(STD[kind]), "tabular": {}, "refs": {}}
    for n, types in fields(children, ("Attribute", "Dimension", "Resource")).items():
        res["fields"][n] = kind_of(types)
        if res["fields"][n] == "ref":
            res["refs"][n] = types
    if kind == "InformationRegister":
        per = kid(props, "InformationRegisterPeriodicity")
        if per is not None and per.text != "Nonperiodical":
            res["fields"]["Период"] = "date"
    for t in children if children is not None else []:
        if local(t.tag) == "TabularSection":
            n = kid(kid(t, "Properties"), "Name").text
            cols = fields(kid(t, "ChildObjects"), ("Attribute",))
            res["tabular"][n] = {c: kind_of(v) for c, v in cols.items()}
            res["refs"].update({f"{n}.{c}": v for c, v in cols.items() if kind_of(v) == "ref"})
    return res


# --- проверка описания --------------------------------------------------------------------------------------

class Spec:
    def __init__(self, spec):
        self.s = spec
        self.config = spec["configPath"]
        self.columns = [c["name"] for c in spec["source"]["columns"]]
        self.params = {p["name"]: p for p in spec.get("params", [])}
        self.lookups = []  # (ключ, таблица запроса, реквизит поиска, вид значения поиска)

    def check_value(self, value, target_kind, where):
        """Значение сопоставления: "Колонка", "param:Имя" или {"lookup", "by", "value"}."""
        if isinstance(value, dict):
            if "lookup" not in value or "value" not in value:
                fail(f"{where}: поиск ссылки задается {{\"lookup\": \"Catalog.<Имя>\", \"by\": \"<реквизит>\", "
                     f"\"value\": \"<колонка>\"}}.")
            target = read_object(self.config, value["lookup"])
            by = value.get("by", "Наименование")
            if by not in target["fields"]:
                fail(f"{where}: у {value['lookup']} нет реквизита '{by}' для поиска. Реквизиты: "
                     f"{', '.join(sorted(target['fields']))}.")
            self.check_value(value["value"], "string", where)
            key = f"{value['lookup']}|{by}"
            if key not in [l[0] for l in self.lookups]:
                self.lookups.append((key, f"{QUERY_TABLES[target['kind']]}.{target['name']}", by,
                                     target["fields"][by]))
            return
        if value.startswith("param:"):
            if value[6:] not in self.params:
                fail(f"{where}: параметра формы '{value[6:]}' нет в params.")
            return
        if value not in self.columns:
            fail(f"{where}: колонки '{value}' нет в source.columns ({', '.join(self.columns)}).")
        if target_kind == "ref":
            fail(f"{where}: реквизит ссылочного типа заполняется поиском ссылки (lookup), а не строкой из колонки.")

    def check_set(self, mapping, fields, owner):
        for attr, value in mapping.items():
            if attr not in fields:
                fail(f"У {owner} нет реквизита '{attr}'. Реквизиты: {', '.join(sorted(fields))}. Если нужных данных "
                     f"в конфигурации нет - не выдумывай реквизит, загрузи остальное и сообщи пользователю, что это "
                     f"значение сохранить некуда.")
            self.check_value(value, fields[attr], f"{owner}.{attr}")


def validate(sp):
    s = sp.s
    fmt = s["source"].get("format")
    if fmt not in FORMATS:
        fail("source.format - csv, excel или json.")
    for c in s["source"]["columns"]:
        if fmt == "json" and "key" not in c:
            fail(f"Колонка '{c['name']}': для JSON укажи key - имя поля объекта в файле.")
        if fmt != "json" and not isinstance(c.get("col"), int):
            fail(f"Колонка '{c['name']}': для CSV и Excel укажи col - номер колонки с 1.")
        if not re.match(r"^[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*$", c["name"]):
            fail(f"Имя колонки '{c['name']}' - идентификатор без пробелов: оно становится ключом структуры.")
    for p in s.get("params", []):
        if p["type"] not in ("date", "string", "number") and not p["type"].startswith("CatalogRef."):
            fail(f"Параметр '{p['name']}': тип date, string, number или CatalogRef.<Имя>.")
        if p["type"].startswith("CatalogRef."):
            read_object(sp.config, "Catalog." + p["type"].split(".", 1)[1])
    t = s["target"]
    obj = read_object(sp.config, t["object"])
    sp.target = obj
    if t["kind"] == "catalog":
        if obj["kind"] != "Catalog":
            fail("target.kind catalog - object Catalog.<Имя>.")
        if t.get("mode", "upsert") not in ("upsert", "create", "update"):
            fail("target.mode - upsert, create или update.")
        key = t["key"]
        if key["attribute"] not in obj["fields"]:
            fail(f"Ключа поиска '{key['attribute']}' у {t['object']} нет. Реквизиты: {', '.join(sorted(obj['fields']))}.")
        sp.check_value(key["value"], obj["fields"][key["attribute"]], "target.key")
        sp.lookups.append((f"{t['object']}|{key['attribute']}", f"Справочник.{obj['name']}", key["attribute"],
                           obj["fields"][key["attribute"]]))
        sp.check_set(t.get("set", {}), obj["fields"], t["object"])
    elif t["kind"] == "register":
        if obj["kind"] != "InformationRegister":
            fail("target.kind register - object InformationRegister.<Имя>.")
        sp.check_set(t["set"], obj["fields"], t["object"])
        if "Период" in obj["fields"] and "Период" not in t["set"]:
            fail(f"{t['object']} периодический: в set нужен Период (например \"param:Дата\").")
    elif t["kind"] == "document":
        if obj["kind"] != "Document":
            fail("target.kind document - object Document.<Имя>.")
        sp.check_set(t.get("header", {}), obj["fields"], t["object"])
        if t["table"] not in obj["tabular"]:
            fail(f"Табличной части '{t['table']}' у {t['object']} нет. Табличные части: "
                 f"{', '.join(sorted(obj['tabular'])) or 'нет'}.")
        sp.check_set(t["rows"], obj["tabular"][t["table"]], f"{t['object']}.{t['table']}")
        if t.get("groupBy") and t["groupBy"] not in sp.columns:
            fail(f"groupBy: колонки '{t['groupBy']}' нет в source.columns.")
    else:
        fail("target.kind - catalog, register или document.")


# --- код ----------------------------------------------------------------------------------------------------

def call(head, args, indent):
    """Вызов с двумя и более параметрами - по параметру на строку, скобка на отдельной строке."""
    if len(args) < 2:
        return [f"{indent}{head}({''.join(args)})"]
    return [f"{indent}{head}("] + [f"{indent}\t{a}," for a in args[:-1]] + [f"{indent}\t{args[-1]}", f"{indent})"]


def lookup_var(key):
    """Короткое имя соответствия поиска: строка присваивания укладывается в 120 символов."""
    obj, by = key.split("|")
    name = obj.split(".")[1].lstrip("_")
    name = re.sub(r"^Демо", "", name)
    return "Ссылки" + name + ("" if by == "Наименование" else "По" + by)


def value_expr(sp, value, kind):
    """Выражение BSL для значения сопоставления из строки Строка (структура колонок)."""
    if isinstance(value, dict):
        key = f"{value['lookup']}|{value.get('by', 'Наименование')}"
        return f"{lookup_var(key)}.Получить({source_expr(sp, value['value'], lookup_kind(sp, key))})"
    return source_expr(sp, value, kind)


def lookup_kind(sp, key):
    return next(l[3] for l in sp.lookups if l[0] == key)


def source_expr(sp, value, kind):
    if value.startswith("param:"):
        return value[6:]
    if kind == "number":
        return f"ЧислоИзСтроки(Строка.{value})"
    if kind == "boolean":
        return f"ВРег(Строка.{value}) = \"ИСТИНА\" Или Строка.{value} = \"1\""
    return f"Строка.{value}"


def lookup_functions(sp):
    """Функция поиска ссылок одним запросом на файл: значения колонки -> соответствие значение - ссылка."""
    out = []
    for key, table, by, kind in sp.lookups:
        fname = "Найти" + lookup_var(key)[len("Ссылки"):]
        out += [f"&НаСервереБезКонтекста", f"Функция {fname}(Значения)", "", "\tЗапрос = Новый Запрос;", "\tЗапрос.Текст =",
                "\t\t\"ВЫБРАТЬ РАЗРЕШЕННЫЕ", f"\t\t|\tТаблица.Ссылка КАК Ссылка,", f"\t\t|\tТаблица.{by} КАК Значение",
                "\t\t|ИЗ", f"\t\t|\t{table} КАК Таблица", "\t\t|ГДЕ", f"\t\t|\tТаблица.{by} В (&Значения)",
                "\t\t|\tИ НЕ Таблица.ПометкаУдаления\";"]
        out += call("Запрос.УстановитьПараметр", ["\"Значения\"", "Значения"], "\t") [:-1] + ["\t);"]
        out += ["", "\tРезультат = Новый Соответствие;", "\tРезультатЗапроса = Запрос.Выполнить();",
                "\tВыборка = РезультатЗапроса.Выбрать();", "\tПока Выборка.Следующий() Цикл"]
        out += call("Результат.Вставить", ["Выборка.Значение", "Выборка.Ссылка"], "\t\t")[:-1] + ["\t\t);"]
        out += ["\tКонецЦикла;", "", "\tВозврат Результат;", "", "КонецФункции", ""]
    return out


def collect_values(sp, key, var):
    """Массив значений колонки для поиска ссылок по всем строкам файла."""
    sources = []
    t = sp.s["target"]
    groups = [t.get("set", {}), t.get("header", {}), t.get("rows", {})]
    for g in groups:
        for v in g.values():
            if isinstance(v, dict) and f"{v['lookup']}|{v.get('by', 'Наименование')}" == key:
                sources.append(v["value"])
    if t["kind"] == "catalog" and key == f"{t['object']}|{t['key']['attribute']}":
        sources.append(t["key"]["value"])
    kind = lookup_kind(sp, key)
    short = var[len("Ссылки"):]
    lines = [f"\tЗначения{short} = Новый Массив;", "\tДля Каждого Строка Из Строки Цикл"]
    for src in dict.fromkeys(sources):
        lines.append(f"\t\tЗначения{short}.Добавить({source_expr(sp, src, kind)});")
    lines += ["\tКонецЦикла;", f"\t{var} = Найти{short}(Значения{short});", ""]
    return lines


def reader_function(sp):
    s = sp.s["source"]
    fmt, enc = s["format"], s.get("encoding", "UTF8").upper()
    ext = FORMATS[fmt][0]
    out = ["&НаСервереБезКонтекста", "Функция ПрочитатьСтроки(Адрес)", "", "\tДвоичныеДанные = ПолучитьИзВременногоХранилища(Адрес);",
           f"\tИмяВременногоФайла = ПолучитьИмяВременногоФайла(\"{ext}\");", "\tДвоичныеДанные.Записать(ИмяВременногоФайла);", "",
           "\tСтроки = Новый Массив;"]
    if fmt == "csv":
        delim = s.get("delimiter", ";").replace('"', '""')
        enc_bsl = "КодировкаТекста.UTF8" if enc == "UTF8" else "КодировкаТекста.ANSI"
        out += call("Чтение = Новый ЧтениеТекста", ["ИмяВременногоФайла", enc_bsl], "\t")[:-1] + ["\t);"]
        out += ["\tТекст = Чтение.ПрочитатьСтроку();"]
        if s.get("header", True):
            out += ["\tТекст = Чтение.ПрочитатьСтроку();"]
        out += ["\tПока Текст <> Неопределено Цикл", "\t\tЕсли ПустаяСтрока(Текст) Тогда",
                "\t\t\tТекст = Чтение.ПрочитатьСтроку();", "\t\t\tПродолжить;", "\t\tКонецЕсли;", "",
                f"\t\tЧасти = СтрРазделить(Текст, \"{delim}\", Истина);",
                f"\t\tПока Части.Количество() < {max(c['col'] for c in s['columns'])} Цикл",
                "\t\t\tЧасти.Добавить(\"\");", "\t\tКонецЦикла;", "", "\t\tСтрока = Новый Структура;"]
        for c in s["columns"]:
            # значение в Вставить - выражение в одну строку: строковые функции, без вызовов с двумя параметрами
            out.append(f"\t\tСтрока.Вставить(\"{c['name']}\", СокрЛП(СтрЗаменить(Части[{c['col'] - 1}], \"\"\"\", \"\")));")
        out += ["\t\tСтроки.Добавить(Строка);", "", "\t\tТекст = Чтение.ПрочитатьСтроку();", "\tКонецЦикла;",
                "\tЧтение.Закрыть();"]
    elif fmt == "excel":
        first = 2 if s.get("header", True) else 1
        out += ["\tТабличныйДокумент = Новый ТабличныйДокумент;", "\tТабличныйДокумент.Прочитать(ИмяВременногоФайла);", "",
                f"\tДля НомерСтроки = {first} По ТабличныйДокумент.ВысотаТаблицы Цикл", "\t\tСтрока = Новый Структура;"]
        for c in s["columns"]:
            out += [f"\t\tОбласть = ТабличныйДокумент.Область(\"R\" + Формат(НомерСтроки, \"ЧГ=\") + \"C{c['col']}\");",
                    f"\t\tСтрока.Вставить(\"{c['name']}\", СокрЛП(Область.Текст));"]
        first_col = s["columns"][0]["name"]
        out += [f"\t\tЕсли ПустаяСтрока(Строка.{first_col}) Тогда", "\t\t\tПродолжить;", "\t\tКонецЕсли;",
                "\t\tСтроки.Добавить(Строка);", "\tКонецЦикла;"]
    else:
        out += ["\tЧтение = Новый ЧтениеJSON;", "\tЧтение.ОткрытьФайл(ИмяВременногоФайла);", "\tДанные = ПрочитатьJSON(", "\t\tЧтение,", "\t\tИстина", "\t);",
                "\tЧтение.Закрыть();", "", "\tДля Каждого Элемент Из Данные Цикл", "\t\tСтрока = Новый Структура;"]
        for c in s["columns"]:
            out.append(f"\t\tСтрока.Вставить(\"{c['name']}\", СтрокаИзJSON(Элемент.Получить(\"{c['key']}\")));")
        out += ["\t\tСтроки.Добавить(Строка);", "\tКонецЦикла;"]
    out += ["", "\tУдалитьФайлы(ИмяВременногоФайла);", "", "\tВозврат Строки;", "", "КонецФункции", ""]
    return out


def helper_functions(sp):
    fmt = sp.s["source"]["format"]
    out = ["&НаСервереБезКонтекста", "Функция ЧислоИзСтроки(Текст)", "",
           "\tОписание = Новый ОписаниеТипов(\"Число\");",
           "\tВозврат Описание.ПривестиЗначение(СтрЗаменить(СтрЗаменить(Текст, \" \", \"\"), \",\", \".\"));", "",
           "КонецФункции", ""]
    if fmt == "json":
        out += ["&НаСервереБезКонтекста", "Функция СтрокаИзJSON(Значение)", "", "\tЕсли Значение = Неопределено Тогда",
                "\t\tВозврат \"\";", "\tКонецЕсли;", "", "\tВозврат СокрЛП(Строка(Значение));", "", "КонецФункции", ""]
    return out


def body_catalog(sp):
    t = sp.s["target"]
    obj = sp.target
    mgr = f"{MANAGERS['Catalog']}.{obj['name']}"
    key = t["key"]
    kvar = lookup_var(f"{t['object']}|{key['attribute']}")
    mode = t.get("mode", "upsert")
    out = ["\tСоздано   = 0;", "\tОбновлено = 0;", "\tПропущено = 0;", ""]
    for k, *_ in sp.lookups:
        out += collect_values(sp, k, lookup_var(k))
    out += ["\tНомерСтроки = 0;", "\tДля Каждого Строка Из Строки Цикл", "\t\tНомерСтроки = НомерСтроки + 1;", ""]
    out += lookup_checks(sp, t.get("set", {}), "\t\t")
    key_lookup = f"{t['object']}|{key['attribute']}"
    out += [f"\t\tСсылка = {kvar}.Получить({source_expr(sp, key['value'], lookup_kind(sp, key_lookup))});"]
    if mode == "create":
        out += ["\t\tЕсли Ссылка <> Неопределено Тогда", "\t\t\tПропущено = Пропущено + 1;", "\t\t\tПродолжить;", "\t\tКонецЕсли;",
                f"\t\tЭлемент = {mgr}.СоздатьЭлемент();"]
    elif mode == "update":
        out += ["\t\tЕсли Ссылка = Неопределено Тогда"]
        out += error_call("НомерСтроки", f"Строка.{key['value']}", "\"Не найден элемент для обновления\"", "\t\t\t")
        out += ["\t\t\tПродолжить;", "\t\tКонецЕсли;", "\t\tЭлемент = Ссылка.ПолучитьОбъект();"]
    else:
        out += ["\t\tЕсли Ссылка = Неопределено Тогда", f"\t\t\tЭлемент = {mgr}.СоздатьЭлемент();", "\t\tИначе",
                "\t\t\tЭлемент = Ссылка.ПолучитьОбъект();", "\t\tКонецЕсли;"]
    out += [f"\t\tЭлемент.{key['attribute']} = {source_expr(sp, key['value'], obj['fields'][key['attribute']])};"]
    out += set_lines(sp, "Элемент", t.get("set", {}), obj["fields"], "\t\t")
    out += ["", "\t\tПопытка", "\t\t\tЭлемент.Записать();", "\t\tИсключение"]
    out += error_call("НомерСтроки", f"Строка.{key['value']}", "КраткоеПредставлениеОшибки(ИнформацияОбОшибке())", "\t\t\t")
    out += ["\t\t\tПродолжить;", "\t\tКонецПопытки;", ""]
    if mode == "create":
        out += ["\t\tСоздано = Создано + 1;"]
    elif mode == "update":
        out += ["\t\tОбновлено = Обновлено + 1;"]
    else:
        out += ["\t\tЕсли Ссылка = Неопределено Тогда", "\t\t\tСоздано = Создано + 1;", "\t\tИначе", "\t\t\tОбновлено = Обновлено + 1;",
                "\t\tКонецЕсли;"]
    out += ["\tКонецЦикла;", ""]
    out += ["\tВозврат СтрШаблон(", "\t\t\"Создано: %1, обновлено: %2, пропущено (уже есть): %3, ошибок: %4\",", "\t\tСоздано,",
            "\t\tОбновлено,", "\t\tПропущено,", "\t\tОшибки.Количество()", "\t);"]
    return out


def lookup_checks(sp, mapping, indent):
    """Для каждого поиска ссылки - переменная и строка ошибки, если ссылка не найдена."""
    out = []
    for attr, value in mapping.items():
        if not isinstance(value, dict):
            continue
        var = "Значение" + attr
        out.append(f"{indent}{var} = {value_expr(sp, value, 'ref')};")
        out += [f"{indent}Если {var} = Неопределено Тогда"]
        out += error_call("НомерСтроки", source_expr(sp, value["value"], "string"),
                          f"\"Не найдено: {value['lookup'].split('.')[1]}.{value.get('by', 'Наименование')}\"", indent + "\t")
        out += [f"{indent}\tПродолжить;", f"{indent}КонецЕсли;", ""]
    return out


def set_lines(sp, target, mapping, fields, indent):
    out = []
    for attr, value in mapping.items():
        if isinstance(value, dict):
            out.append(f"{indent}{target}.{attr} = Значение{attr};")
        else:
            out.append(f"{indent}{target}.{attr} = {source_expr(sp, value, fields[attr])};")
    return out


def error_call(row, value, message, indent):
    return [f"{indent}ДобавитьОшибку(", f"{indent}\t{row},", f"{indent}\t{value},", f"{indent}\t{message}", f"{indent});"]


def body_register(sp):
    t = sp.s["target"]
    obj = sp.target
    out = ["\tЗаписано = 0;", ""]
    for k, *_ in sp.lookups:
        out += collect_values(sp, k, lookup_var(k))
    out += ["\tНомерСтроки = 0;", "\tДля Каждого Строка Из Строки Цикл", "\t\tНомерСтроки = НомерСтроки + 1;", ""]
    out += lookup_checks(sp, t["set"], "\t\t")
    out += [f"\t\tМенеджерЗаписи = {MANAGERS['InformationRegister']}.{obj['name']}.СоздатьМенеджерЗаписи();"]
    out += set_lines(sp, "МенеджерЗаписи", t["set"], obj["fields"], "\t\t")
    first = list(t["set"].values())[0]
    first_src = first["value"] if isinstance(first, dict) else first
    shown = source_expr(sp, first_src, "string") if not first_src.startswith("param:") else "\"\""
    out += ["", "\t\tПопытка", "\t\t\tМенеджерЗаписи.Записать();", "\t\tИсключение"]
    out += error_call("НомерСтроки", shown, "КраткоеПредставлениеОшибки(ИнформацияОбОшибке())", "\t\t\t")
    out += ["\t\t\tПродолжить;", "\t\tКонецПопытки;", "", "\t\tЗаписано = Записано + 1;", "\tКонецЦикла;", ""]
    out += ["\tВозврат СтрШаблон(", "\t\t\"Записано: %1, ошибок: %2\",", "\t\tЗаписано,", "\t\tОшибки.Количество()", "\t);"]
    return out


def body_document(sp):
    t = sp.s["target"]
    obj = sp.target
    mgr = f"{MANAGERS['Document']}.{obj['name']}"
    table = t["table"]
    group = t.get("groupBy")
    out = ["\tСоздано = 0;", ""]
    for k, *_ in sp.lookups:
        out += collect_values(sp, k, lookup_var(k))
    out += ["\tГруппы = Новый Соответствие;", "\tНомерСтроки = 0;", "\tДля Каждого Строка Из Строки Цикл",
            "\t\tНомерСтроки = НомерСтроки + 1;", ""]
    out += lookup_checks(sp, {**t.get("header", {}), **t["rows"]}, "\t\t")
    out += [f"\t\tКлючГруппы = {('Строка.' + group) if group else '\"\"'};", "\t\tСтрокиГруппы = Группы.Получить(КлючГруппы);",
            "\t\tЕсли СтрокиГруппы = Неопределено Тогда", "\t\t\tСтрокиГруппы = Новый Массив;"]
    out += call("Группы.Вставить", ["КлючГруппы", "СтрокиГруппы"], "\t\t\t")[:-1] + ["\t\t\t);"]
    out += ["\t\tКонецЕсли;", "\t\tСтрокиГруппы.Добавить(Строка);", "\tКонецЦикла;", "",
            "\tДля Каждого Группа Из Группы Цикл", "\t\tПерваяСтрока = Группа.Значение[0];", "",
            f"\t\tДокумент = {mgr}.СоздатьДокумент();"]
    if "Дата" not in t.get("header", {}):
        out.append("\t\tДокумент.Дата = ТекущаяДатаСеанса();")
    for attr, value in t.get("header", {}).items():
        expr = value_expr(sp, value, obj["fields"][attr]).replace("Строка.", "ПерваяСтрока.")
        out.append(f"\t\tДокумент.{attr} = {expr};")
    out += ["", "\t\tДля Каждого Строка Из Группа.Значение Цикл", f"\t\t\tСтрокаТаблицы = Документ.{table}.Добавить();"]
    out += set_lines(sp, "СтрокаТаблицы", {a: (v if not isinstance(v, dict) else None) for a, v in t["rows"].items()
                                           if not isinstance(v, dict)}, obj["tabular"][table], "\t\t\t")
    for attr, value in t["rows"].items():
        if isinstance(value, dict):
            out.append(f"\t\t\tСтрокаТаблицы.{attr} = {value_expr(sp, value, 'ref')};")
    write_mode = "РежимЗаписиДокумента.Проведение" if t.get("post") else "РежимЗаписиДокумента.Запись"
    out += ["\t\tКонецЦикла;", "", "\t\tПопытка", f"\t\t\tДокумент.Записать({write_mode});", "\t\tИсключение"]
    out += error_call("0", "Группа.Ключ", "КраткоеПредставлениеОшибки(ИнформацияОбОшибке())", "\t\t\t")
    out += ["\t\t\tПродолжить;", "\t\tКонецПопытки;", "", "\t\tСоздано = Создано + 1;", "\tКонецЦикла;", ""]
    out += ["\tВозврат СтрШаблон(", "\t\t\"Создано документов: %1, ошибок: %2\",", "\t\tСоздано,", "\t\tОшибки.Количество()", "\t);"]
    return out


def form_module(sp):
    s = sp.s
    title = s.get("dialogTitle", "Выберите файл для загрузки").replace('"', '""')
    filt = FORMATS[s["source"]["format"]][1]
    required = [p for p in s.get("params", []) if p.get("required", True)]
    out = ["#Область ОбработчикиКомандФормы", "", "&НаКлиенте", "Процедура ЗагрузитьИзФайла(Команда)", ""]
    for p in required:
        out += [f"\tЕсли НЕ ЗначениеЗаполнено({p['name']}) Тогда",
                f"\t\tОбщегоНазначенияКлиент.СообщитьПользователю(\"Заполните поле \"\"{p.get('title', p['name'])}\"\"\");",
                "\t\tВозврат;", "\tКонецЕсли;", ""]
    out += ["\tПараметрыЗагрузки = ФайловаяСистемаКлиент.ПараметрыЗагрузкиФайла();",
            "\tПараметрыЗагрузки.ИдентификаторФормы = УникальныйИдентификатор;",
            f"\tПараметрыЗагрузки.Диалог.Заголовок = \"{title}\";", f"\tПараметрыЗагрузки.Диалог.Фильтр = \"{filt}\";", ""]
    out += call("Оповещение = Новый ОписаниеОповещения", ["\"ЗагрузитьИзФайлаЗавершение\"", "ЭтотОбъект"], "\t")[:-1] + ["\t);"]
    out += call("ФайловаяСистемаКлиент.ЗагрузитьФайл", ["Оповещение", "ПараметрыЗагрузки"], "\t")[:-1] + ["\t);"]
    out += ["", "КонецПроцедуры", "", "#КонецОбласти", "", "#Область СлужебныеПроцедурыИФункции", ""]
    out += ["&НаСервере", "Функция ЗагрузитьНаСервере(Адрес)", "", "\tОшибки.Очистить();", "\tСтроки = ПрочитатьСтроки(Адрес);", ""]
    kind = s["target"]["kind"]
    out += {"catalog": body_catalog, "register": body_register, "document": body_document}[kind](sp)
    out += ["", "КонецФункции", "", "&НаСервере", "Процедура ДобавитьОшибку(НомерСтроки, Значение, Сообщение)", "",
            "\tСтрокаОшибки = Ошибки.Добавить();", "\tСтрокаОшибки.НомерСтроки = НомерСтроки;",
            "\tСтрокаОшибки.Значение    = Значение;", "\tСтрокаОшибки.Сообщение   = Сообщение;", "", "КонецПроцедуры", ""]
    out += reader_function(sp) + lookup_functions(sp) + helper_functions(sp)
    out += ["// Получает адрес файла во временном хранилище и запускает загрузку. Обработчик оповещения",
            "// ФайловаяСистемаКлиент.ЗагрузитьФайл.", "//", "// Параметры:",
            "//  ПомещенныйФайл - Структура, Неопределено - описание помещенного файла или Неопределено при отказе.",
            "//  ДополнительныеПараметры - Произвольный - не используется.", "//", "&НаКлиенте",
            "Процедура ЗагрузитьИзФайлаЗавершение(ПомещенныйФайл, ДополнительныеПараметры) Экспорт", "",
            "\tЕсли ПомещенныйФайл = Неопределено Тогда", "\t\tВозврат;", "\tКонецЕсли;", "",
            "\tИтог = ЗагрузитьНаСервере(ПомещенныйФайл.Хранение);", "\tОбщегоНазначенияКлиент.СообщитьПользователю(Итог);",
            "", "КонецПроцедуры", "", "#КонецОбласти", ""]
    return "\n".join(out)


def form_json(sp):
    s = sp.s
    attrs, elems = [], []
    pname = s["name"]
    attrs.append({"name": "Объект", "type": f"ExternalDataProcessorObject.{pname}", "main": True})
    for p in s.get("params", []):
        t = {"date": "date", "string": "string(150)", "number": "decimal(15,2)"}.get(p["type"], p["type"])
        attrs.append({"name": p["name"], "type": t, "title": p.get("title", p["name"])})
        elems.append({"input": p["name"], "path": p["name"], "title": p.get("title", p["name"])})
    attrs.append({"name": "Ошибки", "type": "ValueTable", "title": "Ошибки загрузки", "columns": [
        {"name": "НомерСтроки", "type": "decimal(10,0)", "title": "Строка"},
        {"name": "Значение", "type": "string(150)", "title": "Значение"},
        {"name": "Сообщение", "type": "string(500)", "title": "Сообщение"}]})
    elems.append({"button": "ЗагрузитьИзФайла", "command": "ЗагрузитьИзФайла", "title": "Загрузить из файла"})
    elems.append({"table": "Ошибки", "path": "Ошибки", "readOnly": True, "columns": [
        {"input": "ОшибкиНомерСтроки", "path": "Ошибки.НомерСтроки"}, {"input": "ОшибкиЗначение", "path": "Ошибки.Значение"},
        {"input": "ОшибкиСообщение", "path": "Ошибки.Сообщение"}]})
    return {"title": s.get("title", pname), "elements": elems, "attributes": attrs,
            "commands": [{"name": "ЗагрузитьИзФайла", "action": "ЗагрузитьИзФайла", "title": "Загрузить из файла"}]}


def write_bsl(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(l if l.strip() else "" for l in text.split("\n"))
    path.write_bytes(("﻿" + text.replace("\n", "\r\n")).encode("utf-8"))


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("-SpecPath", required=True)
    a = ap.parse_args()
    spec_path = Path(a.SpecPath).resolve()
    s = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    for k in ("configPath", "outputDir", "name", "source", "target"):
        if k not in s:
            fail(f"В описании нет ключа '{k}'.")

    def norm(value):
        m = re.match(r"^/([a-zA-Z])(/.*)?$", value)
        if m and sys.platform == "win32":
            value = f"{m.group(1).upper()}:{m.group(2) or '/'}"
        return str(spec_path.parent / value)
    s["configPath"], s["outputDir"] = norm(s["configPath"]), norm(s["outputDir"])
    if not (Path(s["configPath"]) / "Configuration.xml").is_file():
        fail(f"В configPath нет Configuration.xml: {s['configPath']}. Укажи каталог XML-выгрузки конфигурации.")
    if not (Path(s["configPath"]) / "CommonModules" / "ФайловаяСистемаКлиент.xml").is_file():
        fail("В выгрузке нет общего модуля ФайловаяСистемаКлиент - конфигурация без БСП, генератор неприменим. "
             "Сообщи пользователю.")
    if not re.match(r"^[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*$", s["name"]):
        fail("name - имя обработки, идентификатор без пробелов.")
    sp = Spec(s)
    validate(sp)
    src, name = Path(s["outputDir"]), s["name"]
    if (src / f"{name}.xml").exists():
        fail(f"Обработка {name} уже есть в {src}: генератор создает новую обработку, существующую правь вручную.")
    run_skill("1c-epf-scaffold/scripts/init.py", "-Name", name, "-SrcDir", src, *(["-Synonym", s["title"]] if s.get("title") else []))
    run_skill("1c-epf-add-form/scripts/add-form.py", "-ProcessorName", name, "-FormName", "Форма", "-Main", "-SrcDir", src)
    fj = src / f".{name}.form.json"
    fj.write_text(json.dumps(form_json(sp), ensure_ascii=False), encoding="utf-8")
    try:
        run_skill("1c-form-compile/scripts/form-compile.py", "-JsonPath", fj, "-OutputPath",
                  src / name / "Forms" / "Форма" / "Ext" / "Form.xml")
    finally:
        fj.unlink()
    write_bsl(src / name / "Forms" / "Форма" / "Ext" / "Form" / "Module.bsl", form_module(sp))
    t = s["target"]
    print(f"[OK] Обработка {name}: загрузка {s['source']['format']} -> {t['object']} ({t['kind']})")
    print(f"     {src / (name + '.xml')}")
    print(f"     {src / name / 'Forms' / 'Форма' / 'Ext' / 'Form' / 'Module.bsl'}")
    print("Дальше: собери обработку навыком 1c-epf-build; разбор значений и правила записи при необходимости допиши в "
          "модуле формы.")


if __name__ == "__main__":
    main()
