# file: print-form-gen.py
# Генератор печатной формы по БСП (подсистема Печать) из описания JSON: модель описывает, ЧТО печатать (объект,
# поля шапки, табличная часть, итоги, сумма прописью), генератор сверяет поля с выгрузкой конфигурации и собирает
# каркас по проверенным образцам навыка 1c-print-form:
#   variant "extension" - печать типового документа или справочника в расширении (заимствование объекта, макет,
#     модуль менеджера: перехват &После, если у объекта есть ДобавитьКомандыПечати и Печать; иначе свои процедуры и
#     при необходимости регистрация объекта в УправлениеПечатьюПереопределяемый);
#   variant "external" - внешняя печатная форма (дополнительная обработка БСП): обработка, макет, модуль объекта с
#     СведенияОВнешнейОбработке и Печать с четырьмя параметрами.
# Поле, которого нет в выгрузке, - отказ со списком имеющихся: недостающие данные не выдумываются (без генератора:
# провалы "подход и полнота" и "контракты конфигурации" - 36 из 62).
# Запуск: python print-form-gen.py -SpecPath <spec.json>
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
KINDS = {"Document": ("Documents", "Документ", "Документы"), "Catalog": ("Catalogs", "Справочник", "Справочники")}
KIND_DIRS = {"Catalogs", "Documents", "DataProcessors", "Reports", "InformationRegisters", "AccumulationRegisters",
             "CommonModules", "CommonForms", "CommonTemplates", "Enums", "Constants", "ExchangePlans", "Ext", "Languages",
             "Roles", "Subsystems"}
STD = {"Document": {"Номер", "Дата", "Ссылка", "Проведен", "ПометкаУдаления"},
       "Catalog": {"Код", "Наименование", "Ссылка", "Родитель", "Владелец", "ПометкаУдаления"}}


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
    script = SKILLS / rel
    p = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        fail(f"{rel}: код {p.returncode}\n{(p.stdout + p.stderr)[-1500:]}")
    return p.stdout


def local(tag):
    return tag.rsplit("}", 1)[-1]


def kid(el, name):
    return next((c for c in el if local(c.tag) == name), None) if el is not None else None


def read_object(config, kind, name):
    """Реквизиты (имя -> типы) и табличные части (имя -> {колонка: типы}) объекта из XML-выгрузки."""
    path = Path(config) / KINDS[kind][0] / f"{name}.xml"
    if not path.is_file():
        fail(f"Объекта {kind}.{name} нет в выгрузке ({path}). Сверь имя по выгрузке конфигурации; синоним и имя "
             f"различаются (в УТ 11.5 поступление - ПриобретениеТоваровУслуг).")
    root = ET.parse(path).getroot()
    obj = next(iter(root))
    children = kid(obj, "ChildObjects")

    def attrs(el):
        out = {}
        for a in el if el is not None else []:
            if local(a.tag) != "Attribute":
                continue
            props = kid(a, "Properties")
            n = kid(props, "Name")
            types = [t.text.strip().split(":")[-1] for t in a.iter()
                     if local(t.tag) == "Type" and t.text and t.text.strip()]
            if n is not None:
                out[n.text] = types
        return out
    tss = {}
    for t in children if children is not None else []:
        if local(t.tag) == "TabularSection":
            n = kid(kid(t, "Properties"), "Name")
            tss[n.text] = attrs(kid(t, "ChildObjects"))
    return {"attributes": attrs(children), "tabular": tss, "std": STD[kind]}


def check_field(config, meta, field, where):
    """Поле шапки: реквизит, стандартный реквизит или путь через ссылку (Контрагент.ИНН)."""
    first, _, rest = field.partition(".")
    if first not in meta["attributes"] and first not in meta["std"]:
        fail(f"Поля {where} '{first}' нет в объекте. Реквизиты: {', '.join(sorted(meta['attributes']))}. "
             f"Если нужных данных в конфигурации нет - не выдумывай реквизит, сообщи пользователю об ограничении.")
    if rest:
        types = meta["attributes"].get(first, [])
        ref = next((t for t in types if re.match(r"(Catalog|Document)Ref\.", t)), None)
        if not ref:
            fail(f"'{first}' не ссылка на справочник или документ - путь '{field}' построить нельзя.")
        k, n = ref.split("Ref.")
        sub = read_object(config, k, n)
        second = rest.split(".")[0]
        if second not in sub["attributes"] and second not in sub["std"]:
            fail(f"У {k}.{n} нет реквизита '{second}' (путь '{field}'). Реквизиты: "
                 f"{', '.join(sorted(sub['attributes']))}.")


def alias(field):
    return field.replace(".", "")


def module_methods(path):
    text = path.read_text(encoding="utf-8-sig", errors="replace") if path.is_file() else ""
    return set(re.findall(r"(?im)^\s*Процедура\s+([\wА-Яа-яЁё]+)\s*\([^)]*\)\s*Экспорт", text))


def overridable_registers(config, kind, name):
    p = Path(config) / "CommonModules" / "УправлениеПечатьюПереопределяемый" / "Ext" / "Module.bsl"
    text = p.read_text(encoding="utf-8-sig", errors="replace") if p.is_file() else ""
    mgr = KINDS[kind][2]
    return re.search(rf"ОбъектыПечати\.Добавить\(\s*(?:Метаданные\.)?{mgr}\.{re.escape(name)}\s*\)", text) is not None


def has_amount_in_words(config):
    p = Path(config) / "CommonModules" / "РаботаСКурсамиВалют" / "Ext" / "Module.bsl"
    return p.is_file() and "Функция СформироватьСуммуПрописью(" in p.read_text(encoding="utf-8-sig", errors="replace")


# --- макет --------------------------------------------------------------------------------------------------

def template_json(spec, kind):
    cols = spec.get("table", {}).get("columns", [])
    n = max(4, 1 + len(cols))
    widths = {"1": "1x"}
    for i, c in enumerate(cols, start=2):
        widths[str(i)] = "6x" if i == 2 else "2x"
    for i in range(len(cols) + 2, n + 1):
        widths[str(i)] = "2x"
    label_span = max(1, n // 3)
    areas = [{"name": "Заголовок", "rows": [{"height": 20, "cells": [
        {"col": 1, "span": n, "style": "title", "param": "ТекстЗаголовка"}]}]}]
    head = [{"cells": [{"col": 1, "span": label_span, "style": "label", "text": h.get("title", h["field"]) + ":"},
                       {"col": 1 + label_span, "span": n - label_span, "param": alias(h["field"])}]}
            for h in spec.get("header", [])]
    if head:
        areas.append({"name": "Шапка", "rows": head})
    if cols:
        areas.append({"name": "ШапкаТаблицы", "rows": [{"rowStyle": "cell", "cells": [
            {"col": 1, "style": "cell", "text": "№"}] + [
            {"col": i, "style": "cell", "text": c.get("title", c["field"])} for i, c in enumerate(cols, start=2)]}]})
        areas.append({"name": "Строка", "rows": [{"rowStyle": "cell", "cells": [
            {"col": 1, "style": "cell", "param": "НомерСтроки"}] + [
            {"col": i, "style": "cell", "param": c["field"]} for i, c in enumerate(cols, start=2)]}]})
        totals = [(i, c) for i, c in enumerate(cols, start=2) if c.get("total")]
        if totals:
            areas.append({"name": "Подвал", "rows": [{"cells": [
                {"col": 1, "span": i - 1, "style": "total", "text": f"Итого {c.get('title', c['field']).lower()}:"},
                {"col": i, "style": "total", "param": "Итого" + c["field"]}]} for i, c in totals]})
    if spec.get("amountInWords"):
        areas.append({"name": "СуммаПрописью", "rows": [{"cells": [
            {"col": 1, "span": n, "param": "СуммаПрописью"}]}]})
    return {"columns": n, "page": "A4-portrait", "columnWidths": widths,
            "fonts": {"title": {"face": "Arial", "size": 12, "bold": True}, "bold": {"face": "Arial", "size": 10,
                                                                                       "bold": True}},
            "styles": {"title": {"font": "title"}, "label": {"font": "bold"}, "cell": {"border": "all"},
                       "total": {"font": "bold", "align": "right"}},
            "areas": areas}


# --- код ----------------------------------------------------------------------------------------------------

def query_text(spec, kind, name):
    meta_name = KINDS[kind][1]
    fields = ["ДанныеОбъекта.Ссылка КАК Ссылка"]
    if kind == "Document":
        fields += ["ДанныеОбъекта.Номер КАК Номер", "ДанныеОбъекта.Дата КАК Дата"]
    seen = {"Ссылка", "Номер", "Дата"}
    for h in spec.get("header", []):
        if alias(h["field"]) not in seen:
            fields.append(f"ДанныеОбъекта.{h['field']} КАК {alias(h['field'])}")
            seen.add(alias(h["field"]))
    aw = spec.get("amountInWords") or {}
    table = spec.get("table")
    totals = [c["field"] for c in (table or {}).get("columns", []) if c.get("total")]
    # сумма прописью из реквизита шапки, которого нет в header, тоже выбирается запросом: иначе обращение
    # Шапка.<поле> падает при печати (СуммаДокумента не попадала в выборку)
    if aw.get("field") and aw["field"] not in totals and alias(aw["field"]) not in seen:
        fields.append(f"ДанныеОбъекта.{aw['field']} КАК {alias(aw['field'])}")
        seen.add(alias(aw["field"]))
    if aw.get("currency") and aw["currency"] not in seen:
        fields.append(f"ДанныеОбъекта.{aw['currency']} КАК {aw['currency']}")
    lines = []
    for i, f in enumerate(fields):
        lines.append(f"|	{f}" + ("," if i < len(fields) - 1 or table else ""))
    if table:
        lines.append(f"|	ДанныеОбъекта.{table['section']}.(")
        cols = ["НомерСтроки КАК НомерСтроки"] + [f"{c.get('expr', c['field'])} КАК {c['field']}"
                                                   for c in table["columns"]]
        for i, c in enumerate(cols):
            lines.append(f"|		{c}" + ("," if i < len(cols) - 1 else ""))
        lines.append("|	) КАК СтрокиТаблицы")
    body = "\n\t\t".join(["\"ВЫБРАТЬ РАЗРЕШЕННЫЕ"] + lines + ["|ИЗ", f"|	{meta_name}.{name} КАК ДанныеОбъекта",
                                                               "|ГДЕ",
                                                               "|	ДанныеОбъекта.Ссылка В (&МассивОбъектов)\";"])
    return body


def builder_function(spec, kind, name, func, template_expr, amount_bsp):
    table = spec.get("table")
    cols = table["columns"] if table else []
    totals = [c["field"] for c in cols if c.get("total")]
    title = spec["title"].replace('"', '""')
    out = [f"Функция {func}(МассивОбъектов, ОбъектыПечати)", "\t", "\tЗапрос = Новый Запрос;", "\tЗапрос.Текст =",
           "\t\t" + query_text(spec, kind, name), "\tЗапрос.УстановитьПараметр(", "\t\t\"МассивОбъектов\",", "\t\tМассивОбъектов", "\t);",
           "\t", "\tРезультатЗапроса = Запрос.Выполнить();", "\tШапка = РезультатЗапроса.Выбрать();", "\t",
           "\tТабличныйДокумент = Новый ТабличныйДокумент;",
           f"\tТабличныйДокумент.КлючПараметровПечати = \"ПАРАМЕТРЫ_ПЕЧАТИ_{spec['id']}\";", "\t",
           f"\tМакет = {template_expr};", "\t", "\tПока Шапка.Следующий() Цикл", "\t\t",
           "\t\tЕсли ТабличныйДокумент.ВысотаТаблицы > 0 Тогда",
           "\t\t\tТабличныйДокумент.ВывестиГоризонтальныйРазделительСтраниц();", "\t\tКонецЕсли;", "\t\t",
           "\t\tНомерСтрокиНачало = ТабличныйДокумент.ВысотаТаблицы + 1;", "\t\t"]
    if kind == "Document":
        out += ["\t\tТекстЗаголовка = СтрШаблон(", f"\t\t\t\"{title} № %1 от %2\",", "\t\t\tШапка.Номер,",
                "\t\t\tФормат(Шапка.Дата, \"ДЛФ=D\")", "\t\t);"]
    else:
        out += ["\t\tТекстЗаголовка = СтрШаблон(", f"\t\t\t\"{title}: %1\",", "\t\t\tШапка.Ссылка", "\t\t);"]
    out += ["\t\t", "\t\tОбластьМакета = Макет.ПолучитьОбласть(\"Заголовок\");",
            "\t\tОбластьМакета.Параметры.ТекстЗаголовка = ТекстЗаголовка;", "\t\tТабличныйДокумент.Вывести(ОбластьМакета);",
            "\t\t"]
    if spec.get("header"):
        out += ["\t\tОбластьМакета = Макет.ПолучитьОбласть(\"Шапка\");", "\t\tОбластьМакета.Параметры.Заполнить(Шапка);",
                "\t\tТабличныйДокумент.Вывести(ОбластьМакета);", "\t\t"]
    if table:
        out += ["\t\tИтоги = Новый Структура;"] + [f"\t\tИтоги.Вставить(\"Итого{t}\", 0);" for t in totals] + ["\t\t"] \
            if totals else []
        out += ["\t\tОбластьМакета = Макет.ПолучитьОбласть(\"ШапкаТаблицы\");", "\t\tТабличныйДокумент.Вывести(ОбластьМакета);",
                "\t\t", "\t\tОбластьМакета = Макет.ПолучитьОбласть(\"Строка\");", "\t\tСтроки = Шапка.СтрокиТаблицы.Выбрать();",
                "\t\tПока Строки.Следующий() Цикл", "\t\t\tОбластьМакета.Параметры.Заполнить(Строки);",
                "\t\t\tТабличныйДокумент.Вывести(ОбластьМакета);"]
        if totals:
            out += ["\t\t\t"] + [f"\t\t\tИтоги.Итого{t} = Итоги.Итого{t} + Строки.{t};" for t in totals]
        out += ["\t\tКонецЦикла;", "\t\t"]
        if totals:
            out += ["\t\tОбластьМакета = Макет.ПолучитьОбласть(\"Подвал\");",
                    "\t\tЗаполнитьЗначенияСвойств(", "\t\t\tОбластьМакета.Параметры,", "\t\t\tИтоги", "\t\t);",
                    "\t\tТабличныйДокумент.Вывести(ОбластьМакета);", "\t\t"]
    aw = spec.get("amountInWords")
    if aw:
        src = f"Итоги.Итого{aw['field']}" if aw["field"] in totals else f"Шапка.{alias(aw['field'])}"
        out += ["\t\tОбластьМакета = Макет.ПолучитьОбласть(\"СуммаПрописью\");"]
        if amount_bsp and aw.get("currency"):
            out += ["\t\tОбластьМакета.Параметры.СуммаПрописью = РаботаСКурсамиВалют.СформироватьСуммуПрописью(",
                    f"\t\t\t{src},", f"\t\t\tШапка.{aw['currency']}", "\t\t);"]
        else:
            out += ["\t\tОбластьМакета.Параметры.СуммаПрописью = ЧислоПрописью(", f"\t\t\t{src},", "\t\t\t\"Л=ru_RU; ДП=Истина\",",
                    "\t\t\t\"рубль, рубля, рублей, м, копейка, копейки, копеек, ж, 2\"", "\t\t);"]
        out += ["\t\tТабличныйДокумент.Вывести(ОбластьМакета);", "\t\t"]
    out += ["\t\tУправлениеПечатью.ЗадатьОбластьПечатиДокумента(", "\t\t\tТабличныйДокумент,", "\t\t\tНомерСтрокиНачало,",
            "\t\t\tОбъектыПечати,", "\t\t\tШапка.Ссылка", "\t\t);", "\t\t", "\tКонецЦикла;", "\t", "\tВозврат ТабличныйДокумент;", "\t",
            "КонецФункции"]
    return "\n".join(l if l.strip() else l for l in out)


def add_command(spec, indent="\t"):
    t = spec["title"].replace('"', '""')
    return [f"{indent}КомандаПечати = КомандыПечати.Добавить();",
            f"{indent}КомандаПечати.Идентификатор                  = \"{spec['id']}\";",
            f"{indent}КомандаПечати.Представление                  = \"{t}\";",
            f"{indent}КомандаПечати.ПроверкаПроведенияПередПечатью = Ложь;",
            f"{indent}КомандаПечати.Порядок                        = 90;"]


def print_block(spec, ident_expr, template_path, func):
    t = spec["title"].replace('"', '""')
    # вызовы с двумя и более параметрами - по параметру на строку, скобка на отдельной строке; в условии Если -
    # переменная, а не вызов (правила оформления среды агента, хук check-bsl-style)
    lines = ["\tНужноПечатать = УправлениеПечатью.НужноПечататьМакет(", "\t\tКоллекцияПечатныхФорм,",
             f"\t\t{ident_expr}", "\t);", "\tЕсли НужноПечатать Тогда",
             "\t\tУправлениеПечатью.ВывестиТабличныйДокументВКоллекцию(", "\t\t\tКоллекцияПечатныхФорм,",
             f"\t\t\t{ident_expr},", f"\t\t\t\"{t}\",", f"\t\t\t{func}(", "\t\t\t\tМассивОбъектов,",
             "\t\t\t\tОбъектыПечати", "\t\t\t)"]
    if template_path:
        lines[-1] += ","
        lines += ["\t\t\t,", f"\t\t\t\"{template_path}\""]
    return lines + ["\t\t);", "\tКонецЕсли;"]


def gen_extension(spec, kind, name, meta):
    config = spec["configPath"]
    ext = Path(spec["extension"]["path"])
    folder, meta_ru, mgr_ru = KINDS[kind]
    if not (ext / "Configuration.xml").is_file():
        run_skill("1c-cfe-init/scripts/cfe-init.py", "-Name", spec["extension"]["name"], "-OutputDir", ext,
                  "-ConfigPath", config, "-NoRole")
    cfg_text = (ext / "Configuration.xml").read_text(encoding="utf-8-sig")
    m = re.search(r"<NamePrefix>([^<]*)</NamePrefix>", cfg_text)
    prefix = m.group(1) if m and m.group(1) else spec["id"].split("_")[0] + "_"
    if not (ext / folder / f"{name}.xml").is_file():
        run_skill("1c-cfe-borrow/scripts/cfe-borrow.py", "-ExtensionPath", ext, "-ConfigPath", config,
                  "-Object", f"{kind}.{name}")
    tname = "ПФ_MXL_" + spec["id"]
    if not (ext / folder / name / "Templates" / f"{tname}.xml").is_file():
        run_skill("1c-template-add/scripts/add-template.py", "-ObjectName", name, "-TemplateName", tname,
                  "-TemplateType", "SpreadsheetDocument", "-SrcDir", ext / folder)
    tpl_json = Path(ext) / f".{spec['id']}.mxl.json"
    tpl_json.write_text(json.dumps(template_json(spec, kind), ensure_ascii=False), encoding="utf-8")
    try:
        run_skill("1c-mxl-compile/scripts/mxl-compile.py", "-JsonPath", tpl_json, "-OutputPath",
                  ext / folder / name / "Templates" / tname / "Ext" / "Template.xml")
    finally:
        tpl_json.unlink()

    base_mgr = Path(config) / folder / name / "Ext" / "ManagerModule.bsl"
    methods = module_methods(base_mgr)
    mod = ext / folder / name / "Ext" / "ManagerModule.bsl"
    existing = mod.read_text(encoding="utf-8-sig") if mod.is_file() else ""
    if spec["id"] in existing:
        fail(f"В модуле менеджера расширения уже есть команда {spec['id']}: {mod}")
    for meth in ("ДобавитьКомандыПечати", "Печать", "ПриОпределенииНастроекПечати"):
        if f'&После("{meth}")' in existing or re.search(rf"(?m)^Процедура {meth}\(", existing):
            fail(f"В модуле менеджера расширения уже перехвачен или определен {meth} ({mod}): вторую форму добавь "
                 f"в существующий метод вручную по образцу навыка 1c-print-form.")
    template_path = f"{meta_ru}.{name}.{tname}"
    func = prefix + "Сформировать" + re.sub(r"^[A-Za-z]+_", "", spec["id"])
    ident = f"\"{spec['id']}\""
    bsp = has_amount_in_words(config)
    blocks, scheme = [], ""
    if {"ДобавитьКомандыПечати", "Печать"} <= methods:
        scheme = "перехват &После ДобавитьКомандыПечати и Печать"
        blocks.append("\n".join([f"&После(\"ДобавитьКомандыПечати\")", f"Процедура {prefix}ДобавитьКомандыПечати(КомандыПечати)",
                                 "\t"] + add_command(spec) + ["\t", "КонецПроцедуры"]))
        blocks.append("\n".join(["&После(\"Печать\")",
                                 f"Процедура {prefix}Печать(МассивОбъектов, ПараметрыПечати, КоллекцияПечатныхФорм, "
                                 f"ОбъектыПечати, ПараметрыВывода)", "\t"] + print_block(spec, ident, template_path, func)
                                + ["\t", "КонецПроцедуры"]))
    elif "ДобавитьКомандыПечати" in methods or "Печать" in methods:
        fail(f"У {kind}.{name} есть только одна из процедур ДобавитьКомандыПечати и Печать - случай не типовой, "
             f"сделай форму вручную по образцу навыка 1c-print-form.")
    else:
        if "ПриОпределенииНастроекПечати" in methods:
            scheme = "&После ПриОпределенииНастроекПечати + свои ДобавитьКомандыПечати и Печать"
            blocks.append("\n".join(["&После(\"ПриОпределенииНастроекПечати\")",
                                     f"Процедура {prefix}ПриОпределенииНастроекПечати(Настройки)", "\t",
                                     "\tНастройки.ПриДобавленииКомандПечати = Истина;", "\t", "КонецПроцедуры"]))
        else:
            scheme = "свои ПриОпределенииНастроекПечати, ДобавитьКомандыПечати и Печать"
            blocks.append("\n".join(["// Настройки объекта для подсистемы Печать.", "//", "// Параметры:",
                                     "//  Настройки - см. УправлениеПечатью.НастройкиПечатиОбъекта.", "//",
                                     "Процедура ПриОпределенииНастроекПечати(Настройки) Экспорт", "\t",
                                     "\tНастройки.ПриДобавленииКомандПечати = Истина;", "\t", "КонецПроцедуры"]))
        blocks.append("\n".join(["// Заполняет список команд печати.", "//", "// Параметры:",
                                 "//  КомандыПечати - см. УправлениеПечатью.СоздатьКоллекциюКомандПечати.", "//",
                                 "Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт", "\t"] + add_command(spec)
                                + ["\t", "КонецПроцедуры"]))
        blocks.append("\n".join(["// Формирует печатные формы.", "//", "// Параметры:",
                                 "//  МассивОбъектов - Массив - ссылки на печатаемые объекты.",
                                 "//  ПараметрыПечати - Структура - параметры печати.",
                                 "//  КоллекцияПечатныхФорм - ТаблицаЗначений - сформированные формы.",
                                 "//  ОбъектыПечати - СписокЗначений - области объектов в табличном документе.",
                                 "//  ПараметрыВывода - Структура - параметры вывода.", "//",
                                 "Процедура Печать(МассивОбъектов, ПараметрыПечати, КоллекцияПечатныхФорм, ОбъектыПечати, "
                                 "ПараметрыВывода) Экспорт", "\t"] + print_block(spec, ident, template_path, func)
                                + ["\t", "КонецПроцедуры"]))
        if not overridable_registers(config, kind, name):
            scheme += " + регистрация в УправлениеПечатьюПереопределяемый"
            register_object(ext, config, prefix, kind, name)
    code = ["#Если Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение Тогда", "", "#Область ОбработчикиСобытий",
            ""] + ["\n\n".join(blocks), "", "#КонецОбласти", "", "#Область СлужебныеПроцедурыИФункции", "",
                   builder_function(spec, kind, name, func,
                                    f"УправлениеПечатью.МакетПечатнойФормы(\"{template_path}\")", bsp),
                   "", "#КонецОбласти", "", "#КонецЕсли", ""]
    write_bsl(mod, "\n".join(code), existing)
    return scheme, [mod, ext / folder / name / "Templates" / tname / "Ext" / "Template.xml"]


def register_object(ext, config, prefix, kind, name):
    mod_dir = ext / "CommonModules" / "УправлениеПечатьюПереопределяемый"
    if not (ext / "CommonModules" / "УправлениеПечатьюПереопределяемый.xml").is_file():
        run_skill("1c-cfe-borrow/scripts/cfe-borrow.py", "-ExtensionPath", ext, "-ConfigPath", config,
                  "-Object", "CommonModule.УправлениеПечатьюПереопределяемый")
    mod = mod_dir / "Ext" / "Module.bsl"
    existing = mod.read_text(encoding="utf-8-sig") if mod.is_file() else ""
    line = f"\tНастройки.ОбъектыПечати.Добавить({KINDS[kind][2]}.{name});"
    if line in existing:
        return
    if '&После("ПриОпределенииНастроекПечати")' in existing:
        existing = re.sub(r'(&После\("ПриОпределенииНастроекПечати"\)\nПроцедура [^\n]+\n\t?\n)', r"\1" + line + "\n",
                          existing, count=1)
        write_bsl(mod, existing, "")
        return
    code = ["#Область ПрограммныйИнтерфейс", "", "&После(\"ПриОпределенииНастроекПечати\")",
            f"Процедура {prefix}ПриОпределенииНастроекПечати(Настройки)", "\t", line, "\t", "КонецПроцедуры", "",
            "#КонецОбласти", ""]
    write_bsl(mod, "\n".join(code), "")


def write_bsl(path, text, existing):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(l if l.strip() else "" for l in text.split("\n"))
    # байтами: write_text в Windows сам переводит \n в \r\n, и строки получали \r\r\n
    path.write_bytes(("﻿" + text.replace("\n", "\r\n")).encode("utf-8"))


def gen_external(spec, kind, name, meta):
    ext = spec["external"]
    src, pname = Path(ext["outputDir"]), ext["name"]
    if not (src / f"{pname}.xml").is_file():
        run_skill("1c-epf-scaffold/scripts/init.py", "-Name", pname, "-SrcDir", src)
    tname = "ПФ_MXL_" + spec["id"]
    if not (src / pname / "Templates" / f"{tname}.xml").is_file():
        run_skill("1c-template-add/scripts/add-template.py", "-ObjectName", pname, "-TemplateName", tname,
                  "-TemplateType", "SpreadsheetDocument", "-SrcDir", src)
    tpl_json = src / f".{spec['id']}.mxl.json"
    tpl_json.write_text(json.dumps(template_json(spec, kind), ensure_ascii=False), encoding="utf-8")
    try:
        run_skill("1c-mxl-compile/scripts/mxl-compile.py", "-JsonPath", tpl_json, "-OutputPath",
                  src / pname / "Templates" / tname / "Ext" / "Template.xml")
    finally:
        tpl_json.unlink()
    t = spec["title"].replace('"', '""')
    func = "Сформировать" + re.sub(r"^[A-Za-z]+_", "", spec["id"])
    reg = ["// Сведения о внешней обработке для подсистемы ДополнительныеОтчетыИОбработки.", "//",
           "// Возвращаемое значение:", "//  Структура - см. ДополнительныеОтчетыИОбработки.СведенияОВнешнейОбработке.", "//",
           "Функция СведенияОВнешнейОбработке() Экспорт", "\t", "\tМетаданныеОбработки = Метаданные();", "\t",
           "\tПараметрыРегистрации = ДополнительныеОтчетыИОбработки.СведенияОВнешнейОбработке(\"2.2.2.1\");",
           "\tПараметрыРегистрации.Вид    = ДополнительныеОтчетыИОбработкиКлиентСервер.ВидОбработкиПечатнаяФорма();",
           "\tПараметрыРегистрации.Версия = \"1.0\";", "\t",
           f"\tПараметрыРегистрации.Назначение.Добавить(\"{KINDS[kind][1]}.{name}\");", "\t",
           "\tНоваяКоманда = ПараметрыРегистрации.Команды.Добавить();",
           f"\tНоваяКоманда.Представление        = \"{t}\";",
           "\tНоваяКоманда.Идентификатор        = МетаданныеОбработки.Имя;",
           "\tНоваяКоманда.Использование        = ДополнительныеОтчетыИОбработкиКлиентСервер.ТипКомандыВызовСерверногоМетода();",
           "\tНоваяКоманда.ПоказыватьОповещение = Ложь;", "\tНоваяКоманда.Модификатор          = \"ПечатьMXL\";", "\t",
           "\tВозврат ПараметрыРегистрации;", "\t", "КонецФункции"]
    prn = ["// Формирует печатные формы. Вызывается подсистемой Печать (УправлениеПечатью.ПечатьПоВнешнемуИсточнику).", "//",
           "// Параметры:", "//  МассивОбъектов - Массив - ссылки на печатаемые объекты.",
           "//  КоллекцияПечатныхФорм - ТаблицаЗначений - сформированные формы.",
           "//  ОбъектыПечати - СписокЗначений - области объектов в табличном документе.",
           "//  ПараметрыВывода - Структура - параметры вывода.", "//",
           "Процедура Печать(МассивОбъектов, КоллекцияПечатныхФорм, ОбъектыПечати, ПараметрыВывода) Экспорт", "\t",
           "\tИдентификаторКоманды = Метаданные().Имя;"] + print_block(spec, "ИдентификаторКоманды", None, func) + [
           "\t", "КонецПроцедуры"]
    code = ["#Если Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение Тогда", "", "#Область ПрограммныйИнтерфейс",
            "", "\n".join(reg), "", "\n".join(prn), "", "#КонецОбласти", "", "#Область СлужебныеПроцедурыИФункции", "",
            builder_function(spec, kind, name, func, f"ПолучитьМакет(\"{tname}\")", has_amount_in_words(spec["configPath"])),
            "", "#КонецОбласти", "", "#Иначе", "ВызватьИсключение \"Недопустимый вызов объекта на клиенте.\";", "#КонецЕсли", ""]
    mod = src / pname / "Ext" / "ObjectModule.bsl"
    write_bsl(mod, "\n".join(code), "")
    return "внешняя печатная форма (дополнительная обработка БСП)", [src / f"{pname}.xml", mod]


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("-SpecPath", required=True)
    a = ap.parse_args()
    spec_path = Path(a.SpecPath).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    for k in ("variant", "configPath", "object", "id", "title"):
        if k not in spec:
            fail(f"В описании нет ключа '{k}'.")
    # относительные пути - от каталога файла описания; путь Git Bash /c/... - в C:/... (модель
    # указала /c/AI/.../demo2, Python в Windows прочитал его как C:\c\AI\..., и генератор отказал ложным "нет БСП")

    root = base_dir(spec_path, [(spec.get("configPath"), True), ((spec.get("extension") or {}).get("path"), False), ((spec.get("external") or {}).get("outputDir"), False)])

    def norm(value):
        m = re.match(r"^/([a-zA-Z])(/.*)?$", value)
        if m and sys.platform == "win32":
            value = f"{m.group(1).upper()}:{m.group(2) or '/'}"
        return str(root / value)
    spec["configPath"] = norm(spec["configPath"])
    for part, key in (("extension", "path"), ("external", "outputDir")):
        if spec.get(part, {}).get(key):
            spec[part][key] = norm(spec[part][key])
    if spec.get("external", {}).get("outputDir"):
        # внешняя форма в корне выгрузки или в каталогах ее объектов читается как объект конфигурации, и загрузка
        # конфигурации падает (обработка в DataProcessors/)
        cfg, out = Path(spec["configPath"]).resolve(), Path(spec["external"]["outputDir"]).resolve()
        if out == cfg or (cfg in out.parents and out.relative_to(cfg).parts[0] in KIND_DIRS):
            fail(f"external.outputDir {out} внутри выгрузки конфигурации: внешнюю обработку клади в отдельный "
                 f"каталог, например src/ рядом с выгрузкой.")
    if not (Path(spec["configPath"]) / "Configuration.xml").is_file():
        fail(f"В configPath нет Configuration.xml: {spec['configPath']}. Укажи каталог XML-выгрузки конфигурации - "
             f"абсолютный путь вида C:/... или путь относительно файла описания.")
    if not (Path(spec["configPath"]) / "CommonModules" / "УправлениеПечатью.xml").is_file():
        fail("В выгрузке нет общего модуля УправлениеПечатью - конфигурация без подсистемы Печать БСП, генератор "
             "неприменим. Сообщи пользователю.")
    kind, _, name = spec["object"].partition(".")
    if kind not in KINDS:
        fail("object - 'Document.<Имя>' или 'Catalog.<Имя>'.")
    if not re.match(r"^[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*$", spec["id"]):
        fail("id - идентификатор без точек, запятых и пробелов (точка и запятая в идентификаторе команды печати "
             "меняют смысл: менеджер печати, комплект).")
    meta = read_object(spec["configPath"], kind, name)
    for h in spec.get("header", []):
        check_field(spec["configPath"], meta, h["field"], "шапки")
    table = spec.get("table")
    if table:
        if table["section"] not in meta["tabular"]:
            fail(f"Табличной части '{table['section']}' у {spec['object']} нет. Табличные части: "
                 f"{', '.join(sorted(meta['tabular'])) or 'нет'}. Не создавай табличную часть ради печати - сообщи "
                 f"пользователю, каких данных нет, и предложи вариант.")
        cols = meta["tabular"][table["section"]]
        for c in table["columns"]:
            if c.get("expr"):
                # вычисляемая колонка: каждое имя выражения - колонка табличной части
                for ident in re.findall(r"[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*", c["expr"]):
                    if ident not in cols and ident.upper() not in ("ВЫБОР", "КОГДА", "ТОГДА", "ИНАЧЕ", "КОНЕЦ", "ЕСТЬNULL"):
                        fail(f"В выражении колонки '{c['field']}' ({c['expr']}) нет колонки '{ident}' в табличной "
                             f"части {table['section']}. Колонки: {', '.join(sorted(cols))}.")
                continue
            if c["field"] not in cols:
                fail(f"Колонки '{c['field']}' в табличной части {table['section']} нет. Колонки: "
                     f"{', '.join(sorted(cols))}. Нужного значения нет - вычисли из имеющихся (например, Количество * "
                     f"Цена) в коде или сообщи пользователю.")
    aw = spec.get("amountInWords")
    if aw:
        totals = [c["field"] for c in (table or {}).get("columns", []) if c.get("total")]
        if aw["field"] not in totals:
            check_field(spec["configPath"], meta, aw["field"], "суммы прописью")
        if aw.get("currency"):
            check_field(spec["configPath"], meta, aw["currency"], "валюты")
    if spec["variant"] == "extension":
        scheme, files = gen_extension(spec, kind, name, meta)
    elif spec["variant"] == "external":
        scheme, files = gen_external(spec, kind, name, meta)
    else:
        fail("variant - 'extension' или 'external'.")
    print(f"[OK] Печатная форма {spec['id']} ({spec['title']}) для {spec['object']}: {scheme}")
    for f in files:
        print(f"     {f}")
    print("Дальше: проверь поля макета и запроса, при необходимости допиши расчет значений в функции формирования; "
          "внешнюю форму собери навыком 1c-epf-build.")


if __name__ == "__main__":
    main()
