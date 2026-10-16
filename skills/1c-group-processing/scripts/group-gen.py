# file: group-gen.py
# Генератор внешней обработки групповой обработки справочника или документов из описания JSON: параметры на форме,
# отбор объектов (запрос строится по метаданным: период и проведенность документов, условия по реквизитам, отсутствие
# движений по регистру за N дней, без групп и помеченных на удаление), список с флажками, подтверждение и действие
# над отмеченными - замена реквизитов, пометка на удаление, перепроведение. Каждый объект обрабатывается в Попытка с
# блокировкой, проведенный документ записывается с проведением, ошибки собираются в таблицу, итог - сообщением.
# Обработка собирается навыками 1c-epf-scaffold, 1c-epf-add-form, 1c-form-compile.
# Оценка 16.10.2026: решения групповых обработок обращались к реквизитам, которых нет в обработке, клали модуль не по
# тому пути, вызывали Колонки.Добавить у реквизита формы, писали без обработки ошибок и без учета проведенности.
# Запуск: python group-gen.py -SpecPath <spec.json>
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
KINDS = {"Document": ("Documents", "Документ", "ДокументСсылка"), "Catalog": ("Catalogs", "Справочник", "СправочникСсылка")}
REF_RU = {"CatalogRef": ("Catalogs", "Справочник", "Справочники"), "DocumentRef": ("Documents", "Документ", "Документы"),
          "EnumRef": ("Enums", "Перечисление", "Перечисления")}
STD = {"Document": {"Номер": ["string"], "Дата": ["dateTime"], "Проведен": ["boolean"]},
       "Catalog": {"Код": ["string"], "Наименование": ["string"], "Родитель": [], "Владелец": []}}
METADATA_DIRS = {"catalogs", "documents", "dataprocessors", "reports", "informationregisters", "accumulationregisters",
                 "commonmodules", "enums", "ext", "languages", "roles", "subsystems"}
ACTIONS = {"set", "markDeletion", "repost"}


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


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


def read_meta(config, full):
    kind, _, name = full.partition(".")
    folder = {"Document": "Documents", "Catalog": "Catalogs", "AccumulationRegister": "AccumulationRegisters",
              "InformationRegister": "InformationRegisters"}.get(kind)
    if not folder:
        fail(f"'{full}': поддерживаются Document, Catalog (объект) и AccumulationRegister, InformationRegister (движения).")
    path = Path(config) / folder / f"{name}.xml"
    if not path.is_file():
        have = sorted(p.stem for p in (Path(config) / folder).glob("*.xml"))
        fail(f"Объекта {full} нет в выгрузке. {folder}: {', '.join(have[:100])}{' ...' if len(have) > 100 else ''}")
    obj = next(iter(ET.parse(path).getroot()))
    props = kid(obj, "Properties")
    fields = {}
    for c in (kid(obj, "ChildObjects") if kid(obj, "ChildObjects") is not None else []):
        if local(c.tag) in ("Attribute", "Dimension", "Resource"):
            fields[kid(kid(c, "Properties"), "Name").text] = [
                t.text.strip().split(":")[-1] for t in c.iter() if local(t.tag) == "Type" and t.text and t.text.strip()]
    hier = kid(props, "Hierarchical")
    htype = kid(props, "HierarchyType")
    return {"kind": kind, "name": name, "fields": fields,
            "folders": hier is not None and hier.text == "true" and (htype is None or htype.text != "HierarchyOfItems")}


def q(s):
    return '"' + str(s).replace('"', '""') + '"'


def call(head, args, ind=""):
    if len(args) == 1:
        return [f"{ind}{head}({args[0]})"]
    return [f"{ind}{head}("] + [f"{ind}\t{a}," for a in args[:-1]] + [f"{ind}\t{args[-1]}", f"{ind})"]


def write_bsl(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(l if l.strip() else "" for l in text.split("\n"))
    path.write_bytes(("﻿" + text.replace("\n", "\r\n")).encode("utf-8"))


def form_type(t):
    m = re.fullmatch(r"(CatalogRef|DocumentRef|EnumRef)\.(\w+)", t)
    if m:
        return t
    return {"date": "date", "dateTime": "dateTime", "boolean": "boolean", "string": "string(150)",
            "number": "decimal(15,2)"}.get(t) or fail(f"Тип параметра '{t}' не поддержан: date, dateTime, boolean, "
                                                      f"string, number, CatalogRef.X, DocumentRef.X, EnumRef.X.")


class Gen:
    def __init__(self, s):
        self.s = s
        self.config = s["configPath"]
        self.meta = read_meta(self.config, s["object"])
        if self.meta["kind"] not in KINDS:
            fail("object - Document.<Имя> или Catalog.<Имя>.")
        self.params = {p["name"]: p for p in s.get("params", [])}
        for p in self.params.values():
            form_type(p["type"])
            if p["type"].split(".")[0] in REF_RU:
                kind, name = p["type"].split(".")
                if not (Path(self.config) / REF_RU[kind][0] / f"{name}.xml").is_file():
                    fail(f"Параметр {p['name']}: типа {p['type']} нет в выгрузке.")

    def field_types(self, f):
        if f in self.meta["fields"]:
            return self.meta["fields"][f]
        if f in STD[self.meta["kind"]]:
            return STD[self.meta["kind"]][f]
        fail(f"Реквизита '{f}' у {self.s['object']} нет. Реквизиты: {', '.join(sorted(self.meta['fields']))}. "
             f"Нет данных - не выдумывай реквизит, сообщи пользователю.")

    def empty_expr(self, f):
        types = self.field_types(f)
        ref = next((t for t in types if t.split(".")[0] in REF_RU), None)
        if ref:
            kind, name = ref.split(".")
            return f"ЗНАЧЕНИЕ({REF_RU[kind][1]}.{name}.ПустаяСсылка)"
        if types and types[0] == "boolean":
            return "ЛОЖЬ"
        if types and types[0].startswith("decimal"):
            return "0"
        if types and types[0] in ("date", "dateTime"):
            return "ДАТАВРЕМЯ(1, 1, 1)"
        return None

    def query(self):
        s, m = self.s, self.meta
        sel = s.get("selection", {})
        cols = s.get("columns") or (["Дата", "Номер"] if m["kind"] == "Document" else ["Наименование"])
        for c in cols:
            self.field_types(c)
        qparams, batch, where = {}, [], ["НЕ Объекты.ПометкаУдаления"]
        if m["kind"] == "Document":
            if sel.get("period", True):
                where.append("Объекты.Дата МЕЖДУ &НачалоПериода И &КонецПериода")
                qparams["НачалоПериода"] = "НачалоПериода"
                qparams["КонецПериода"] = "КонецДня(КонецПериода)"
            posted = sel.get("posted")
            if posted is True:
                where.append("Объекты.Проведен")
            elif posted is False:
                where.append("НЕ Объекты.Проведен")
        elif m["folders"] and sel.get("excludeGroups", True):
            where.append("НЕ Объекты.ЭтоГруппа")
        for c in sel.get("conditions", []):
            f, op = c["field"], c.get("op", "=")
            if op == "empty":
                expr = self.empty_expr(f)
                if expr:
                    where.append(f"Объекты.{f} = {expr}")
                else:
                    where.append(f"Объекты.{f} = &Пустое{f}")
                    qparams[f"Пустое{f}"] = q("")
            elif op in ("=", "<>"):
                self.field_types(f)
                if "param" in c:
                    if c["param"] not in self.params:
                        fail(f"Условие {f}: параметра '{c['param']}' нет в params.")
                    where.append(f"Объекты.{f} {op} &{c['param']}")
                    qparams[c["param"]] = c["param"]
                else:
                    v = c.get("value")
                    lit = {True: "ИСТИНА", False: "ЛОЖЬ"}.get(v) if isinstance(v, bool) else None
                    if lit is None:
                        fail(f"Условие {f}: value поддерживается только булево; другое значение задай через param.")
                    where.append(f"Объекты.{f} {op} {lit}")
            else:
                fail(f"Условие {f}: op - '=', '<>' или 'empty'.")
        nm = sel.get("noMovements")
        if nm:
            reg = read_meta(self.config, nm["register"])
            if reg["kind"] not in ("AccumulationRegister", "InformationRegister"):
                fail("noMovements.register - регистр накопления или сведений.")
            dim = nm.get("dimension")
            types = reg["fields"].get(dim)
            if types is None:
                fail(f"noMovements: у {nm['register']} нет поля '{dim}'. Поля: {', '.join(sorted(reg['fields']))}.")
            own = f"{'CatalogRef' if m['kind'] == 'Catalog' else 'DocumentRef'}.{m['name']}"
            if own not in types:
                fail(f"noMovements: поле {dim} регистра имеет тип {', '.join(types)}, а не {own}.")
            ru = {"AccumulationRegister": "РегистрНакопления", "InformationRegister": "РегистрСведений"}[reg["kind"]]
            batch.append("ВЫБРАТЬ РАЗЛИЧНЫЕ\n\tДвижения." + dim + " КАК Объект\nПОМЕСТИТЬ ВТДвижения\nИЗ\n\t"
                         f"{ru}.{reg['name']} КАК Движения\nГДЕ\n\tДвижения.Период >= &ДатаГраницы\n"
                         "ИНДЕКСИРОВАТЬ ПО\n\tОбъект")
            qparams["ДатаГраницы"] = f"ТекущаяДатаСеанса() - {int(nm.get('days', 365))} * 86400"
            where.append("ВТДвижения.Объект ЕСТЬ NULL")
        fields = ",\n".join(["\tОбъекты.Ссылка КАК Ссылка"] + [f"\tОбъекты.{c} КАК {c}" for c in cols])
        frm = f"\t{KINDS[m['kind']][1]}.{m['name']} КАК Объекты"
        if nm:
            frm += "\n\t\tЛЕВОЕ СОЕДИНЕНИЕ ВТДвижения КАК ВТДвижения\n\t\tПО Объекты.Ссылка = ВТДвижения.Объект"
        order = "Объекты.Дата" if m["kind"] == "Document" else "Объекты.Наименование"
        main = (f"ВЫБРАТЬ РАЗРЕШЕННЫЕ\n{fields}\nИЗ\n{frm}\nГДЕ\n\t" + "\n\tИ ".join(where)
                + f"\nУПОРЯДОЧИТЬ ПО\n\t{order}")
        batch = [b.replace("ВЫБРАТЬ РАЗЛИЧНЫЕ", "ВЫБРАТЬ РАЗРЕШЕННЫЕ РАЗЛИЧНЫЕ", 1) for b in batch]
        return "\n;\n\n".join(batch + [main]), qparams, cols

    def action_lines(self):
        s, m = self.s, self.meta
        a = s.get("action") or fail("В описании нет action.")
        kind = a.get("kind")
        if kind not in ACTIONS:
            fail(f"action.kind - {', '.join(sorted(ACTIONS))}.")
        lines = ["ОбъектОбработки = СтрокаСписка.Ссылка.ПолучитьОбъект();", "ОбъектОбработки.Заблокировать();"]
        if kind == "set":
            pairs = []
            for f, v in (a.get("set") or fail("action.set: какие реквизиты чем заполнить.")).items():
                self.field_types(f)
                if isinstance(v, str) and v.startswith("param:"):
                    pn = v[6:]
                    if pn not in self.params:
                        fail(f"action.set {f}: параметра '{pn}' нет в params.")
                    pairs.append((f"ОбъектОбработки.{f}", pn))
                elif isinstance(v, bool):
                    pairs.append((f"ОбъектОбработки.{f}", "Истина" if v else "Ложь"))
                else:
                    fail(f"action.set {f}: значение - \"param:<Имя>\" или булево.")
            width = max(len(l) for l, _ in pairs)
            lines += [f"{l.ljust(width)} = {r};" for l, r in pairs] + [""]
            if m["kind"] == "Document":
                lines += ["Если ОбъектОбработки.Проведен Тогда",
                          "\tОбъектОбработки.Записать(РежимЗаписиДокумента.Проведение);", "Иначе",
                          "\tОбъектОбработки.Записать(РежимЗаписиДокумента.Запись);", "КонецЕсли;"]
            else:
                lines.append("ОбъектОбработки.Записать();")
        elif kind == "markDeletion":
            lines.append("ОбъектОбработки.УстановитьПометкуУдаления(Истина);")
        else:
            if m["kind"] != "Document":
                fail("action repost - только для документов.")
            lines.append("ОбъектОбработки.Записать(РежимЗаписиДокумента.Проведение);")
        return lines, kind

    def module(self):
        s = self.s
        text, qparams, cols = self.query()
        act, kind = self.action_lines()
        required = [p for p in self.params.values() if p.get("required", True)]
        period = self.meta["kind"] == "Document" and s.get("selection", {}).get("period", True)
        confirm = s.get("confirm", True)
        question = s.get("question") or {"set": "Изменить отмеченные объекты?",
                                         "markDeletion": "Пометить на удаление отмеченные объекты?",
                                         "repost": "Перепровести отмеченные документы?"}[kind]
        out = ["#Область ОбработчикиСобытийФормы", "", "&НаСервере",
               "Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)", ""]
        if period:
            out += ["\tНачалоПериода = НачалоМесяца(ТекущаяДатаСеанса());", "\tКонецПериода  = КонецМесяца(ТекущаяДатаСеанса());"]
        else:
            out += ["\tСписок.Очистить();"]
        out += ["", "КонецПроцедуры", "", "#КонецОбласти", "", "#Область ОбработчикиКомандФормы", "",
                "&НаКлиенте", "Процедура Заполнить(Команда)", ""]
        checks = [(p["name"], p.get("title", p["name"])) for p in required if p["name"] in qparams]
        if period:
            checks = [("НачалоПериода", "Начало периода"), ("КонецПериода", "Конец периода")] + checks
        for n, t in checks:
            out += [f"\tЕсли НЕ ЗначениеЗаполнено({n}) Тогда",
                    f"\t\tОбщегоНазначенияКлиент.СообщитьПользователю(\"Заполните поле \"\"{t}\"\"\");",
                    "\t\tВозврат;", "\tКонецЕсли;", ""]
        out += ["\tЗаполнитьНаСервере();", "", "КонецПроцедуры", "", "&НаКлиенте", "Процедура ВыполнитьДействие(Команда)", ""]
        for p in required:
            if p["name"] not in qparams:
                out += [f"\tЕсли НЕ ЗначениеЗаполнено({p['name']}) Тогда",
                        f"\t\tОбщегоНазначенияКлиент.СообщитьПользователю(\"Заполните поле \"\"{p.get('title', p['name'])}\"\"\");",
                        "\t\tВозврат;", "\tКонецЕсли;", ""]
        out += ["\tОтмеченные = Список.НайтиСтроки(Новый Структура(\"Пометка\", Истина));",
                "\tЕсли НЕ ЗначениеЗаполнено(Отмеченные) Тогда",
                "\t\tОбщегоНазначенияКлиент.СообщитьПользователю(\"Отметьте объекты в списке.\");", "\t\tВозврат;",
                "\tКонецЕсли;", ""]
        if confirm:
            out += call("Оповещение = Новый ОписаниеОповещения", ['"ВыполнитьЗавершение"', "ЭтотОбъект"], "\t")[:-1] + ["\t);"]
            out += call("ПоказатьВопрос", ["Оповещение", q(question), "РежимДиалогаВопрос.ДаНет"], "\t")[:-1] + ["\t);"]
        else:
            out += ["\tВыполнитьЗавершение(КодВозвратаДиалога.Да, Неопределено);"]
        out += ["", "КонецПроцедуры", "", "&НаКлиенте", "Процедура ОтметитьВсе(Команда)", "",
                "\tУстановитьПометку(Истина);", "", "КонецПроцедуры", "", "&НаКлиенте", "Процедура СнятьОтметки(Команда)", "",
                "\tУстановитьПометку(Ложь);", "", "КонецПроцедуры", "", "#КонецОбласти", "",
                "#Область СлужебныеПроцедурыИФункции", "", "&НаСервере", "Процедура ЗаполнитьНаСервере()", "",
                "\tЗапрос = Новый Запрос;", "\tЗапрос.Текст ="]
        rows = text.replace('"', '""').split("\n")
        out += ['\t\t"' + rows[0]] + ["\t\t|" + r for r in rows[1:]]
        out[-1] += '";'
        for pn, expr in qparams.items():
            out += call("Запрос.УстановитьПараметр", [q(pn), expr], "\t")[:-1] + ["\t);"]
        out += ["", "\tРезультатЗапроса = Запрос.Выполнить();", "\tВыборка = РезультатЗапроса.Выбрать();", "",
                "\tСписок.Очистить();", "\tПока Выборка.Следующий() Цикл",
                "\t\tСтрокаСписка = Список.Добавить();", "\t\tЗаполнитьЗначенияСвойств(СтрокаСписка, Выборка);",
                "\t\tСтрокаСписка.Пометка = Истина;", "\tКонецЦикла;", "", "КонецПроцедуры", "",
                "&НаСервере", "Функция ВыполнитьНаСервере()", "", "\tОшибки.Очистить();", "\tОбработано = 0;", "",
                "\tДля Каждого СтрокаСписка Из Список Цикл", "\t\tЕсли НЕ СтрокаСписка.Пометка Тогда",
                "\t\t\tПродолжить;", "\t\tКонецЕсли;", "", "\t\tПопытка"]
        out += ["\t\t\t" + l if l else "" for l in act]
        out += ["\t\t\tОбработано = Обработано + 1;", "\t\tИсключение",
                "\t\t\tСтрокаОшибки = Ошибки.Добавить();", "\t\t\tСтрокаОшибки.Ссылка    = СтрокаСписка.Ссылка;",
                "\t\t\tСтрокаОшибки.Сообщение = КраткоеПредставлениеОшибки(ИнформацияОбОшибке());", "",
                "\t\t\tЗаписьЖурналаРегистрации(",
                f"\t\t\t\t{q(s['name'])},", "\t\t\t\tУровеньЖурналаРегистрации.Ошибка,", "\t\t\t\t,",
                "\t\t\t\tСтрокаСписка.Ссылка,", "\t\t\t\tПодробноеПредставлениеОшибки(ИнформацияОбОшибке())", "\t\t\t);",
                "\t\tКонецПопытки;", "\tКонецЦикла;", "", "\tЗаполнитьНаСервере();", ""]
        out += ["\tВозврат СтрШаблон(", f"\t\t{q(s.get('resultTemplate', 'Обработано: %1, ошибок: %2.'))},",
                "\t\tОбработано,", "\t\tОшибки.Количество()", "\t);", "", "КонецФункции", "",
                "// Выполняет действие над отмеченными объектами после подтверждения пользователя. Обработчик оповещения",
                "// ПоказатьВопрос.", "//", "// Параметры:", "//  Ответ - КодВозвратаДиалога - ответ пользователя.",
                "//  ДополнительныеПараметры - Произвольный - не используется.", "//", "&НаКлиенте",
                "Процедура ВыполнитьЗавершение(Ответ, ДополнительныеПараметры) Экспорт", "",
                "\tЕсли Ответ <> КодВозвратаДиалога.Да Тогда", "\t\tВозврат;", "\tКонецЕсли;", "",
                "\tИтог = ВыполнитьНаСервере();", "\tОбщегоНазначенияКлиент.СообщитьПользователю(Итог);", "",
                "КонецПроцедуры", "", "&НаКлиенте", "Процедура УстановитьПометку(Пометка)", "",
                "\tДля Каждого СтрокаСписка Из Список Цикл", "\t\tСтрокаСписка.Пометка = Пометка;", "\tКонецЦикла;", "",
                "КонецПроцедуры", "", "#КонецОбласти", ""]
        return "\n".join(out), cols, period

    def form_json(self, cols, period):
        s, m = self.s, self.meta
        ref = f"{'CatalogRef' if m['kind'] == 'Catalog' else 'DocumentRef'}.{m['name']}"
        attrs = [{"name": "Объект", "type": f"ExternalDataProcessorObject.{s['name']}", "main": True}]
        elems = []
        if period:
            attrs += [{"name": "НачалоПериода", "type": "date", "title": "Начало периода"},
                      {"name": "КонецПериода", "type": "date", "title": "Конец периода"}]
            elems.append({"group": "horizontal", "name": "ГруппаПериод", "showTitle": False, "representation": "none",
                          "children": [{"input": "НачалоПериода", "path": "НачалоПериода", "title": "Период с"},
                                       {"input": "КонецПериода", "path": "КонецПериода", "title": "по"}]})
        for p in self.params.values():
            attrs.append({"name": p["name"], "type": form_type(p["type"]), "title": p.get("title", p["name"])})
            key = "check" if p["type"] == "boolean" else "input"
            elems.append({key: p["name"], "path": p["name"], "title": p.get("title", p["name"])})
        col_attrs = [{"name": "Пометка", "type": "boolean", "title": " "}, {"name": "Ссылка", "type": ref,
                                                                         "title": s.get("objectTitle", "Объект")}]
        for c in cols:
            types = self.field_types(c)
            t = types[0] if types else "string"
            t = t if re.match(r"(Catalog|Document|Enum)Ref\.", t) else {"dateTime": "dateTime", "date": "date",
                                                                         "boolean": "boolean"}.get(t, "string(150)")
            if t.startswith("string") and types and types[0].startswith("decimal"):
                t = types[0]
            col_attrs.append({"name": c, "type": t, "title": c})
        attrs.append({"name": "Список", "type": "ValueTable", "title": "Объекты", "columns": col_attrs})
        attrs.append({"name": "Ошибки", "type": "ValueTable", "title": "Ошибки", "columns": [
            {"name": "Ссылка", "type": ref, "title": "Объект"}, {"name": "Сообщение", "type": "string(1000)",
                                                                 "title": "Ошибка"}]})
        elems.append({"group": "horizontal", "name": "ГруппаКнопки", "showTitle": False, "representation": "none",
                      "children": [{"button": "Заполнить", "command": "Заполнить", "title": "Заполнить"},
                                   {"button": "ВыполнитьДействие", "command": "ВыполнитьДействие", "title": s.get("actionTitle", "Выполнить")},
                                   {"button": "ОтметитьВсе", "command": "ОтметитьВсе", "title": "Отметить все"},
                                   {"button": "СнятьОтметки", "command": "СнятьОтметки", "title": "Снять отметки"}]})
        list_cols = [{"check": "СписокПометка", "path": "Список.Пометка"},
                     {"labelField": "СписокСсылка", "path": "Список.Ссылка"}]
        list_cols += [{"labelField": f"Список{c}", "path": f"Список.{c}"} for c in cols]
        elems.append({"table": "Список", "path": "Список", "columns": list_cols})
        elems.append({"table": "Ошибки", "path": "Ошибки", "readOnly": True, "columns": [
            {"labelField": "ОшибкиСсылка", "path": "Ошибки.Ссылка"},
            {"labelField": "ОшибкиСообщение", "path": "Ошибки.Сообщение"}]})
        cmds = [{"name": n, "action": n, "title": t} for n, t in (("Заполнить", "Заполнить"),
                                                                   ("ВыполнитьДействие", s.get("actionTitle", "Выполнить")),
                                                                   ("ОтметитьВсе", "Отметить все"),
                                                                   ("СнятьОтметки", "Снять отметки"))]
        return {"title": s.get("title", s["name"]), "events": {"OnCreateAtServer": "ПриСозданииНаСервере"},
                "elements": elems, "attributes": attrs, "commands": cmds}


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("-SpecPath", required=True)
    a = ap.parse_args()
    spec_path = Path(a.SpecPath).resolve()
    try:
        s = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        fail(f"Описание {spec_path} - не JSON: {e}")
    for k in ("configPath", "outputDir", "name", "object", "action"):
        if k not in s:
            fail(f"В описании нет ключа '{k}'.")

    def norm(value):
        m = re.match(r"^/([a-zA-Z])(/.*)?$", value)
        if m and sys.platform == "win32":
            value = f"{m.group(1).upper()}:{m.group(2) or '/'}"
        return str(spec_path.parent / value)
    s["configPath"], s["outputDir"] = norm(s["configPath"]), norm(s["outputDir"])
    cfg, out = Path(s["configPath"]).resolve(), Path(s["outputDir"]).resolve()
    if not (cfg / "Configuration.xml").is_file():
        fail(f"В configPath нет Configuration.xml: {cfg}. Укажи каталог XML-выгрузки конфигурации.")
    if out == cfg or (cfg in out.parents and out.relative_to(cfg).parts[0].lower() in METADATA_DIRS):
        fail(f"outputDir {out} внутри выгрузки конфигурации: внешнюю обработку клади в отдельный каталог, например src/.")
    if not (cfg / "CommonModules" / "ОбщегоНазначенияКлиент.xml").is_file():
        fail("В выгрузке нет ОбщегоНазначенияКлиент - конфигурация без БСП, генератор неприменим. Сообщи пользователю.")
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*", s["name"]):
        fail("name - имя обработки, идентификатор без пробелов.")
    g = Gen(s)
    text, cols, period = g.module()
    form = g.form_json(cols, period)
    name = s["name"]
    if (out / f"{name}.xml").exists():
        fail(f"Обработка {name} уже есть в {out}: генератор создает новую обработку.")
    run_skill("1c-epf-scaffold/scripts/init.py", "-Name", name, "-SrcDir", out, *(["-Synonym", s["title"]] if s.get("title") else []))
    run_skill("1c-epf-add-form/scripts/add-form.py", "-ProcessorName", name, "-FormName", "Форма", "-Main", "-SrcDir", out)
    fj = out / f".{name}.form.json"
    fj.write_text(json.dumps(form, ensure_ascii=False), encoding="utf-8")
    try:
        run_skill("1c-form-compile/scripts/form-compile.py", "-JsonPath", fj, "-OutputPath",
                  out / name / "Forms" / "Форма" / "Ext" / "Form.xml")
    finally:
        fj.unlink()
    mod = out / name / "Forms" / "Форма" / "Ext" / "Form" / "Module.bsl"
    write_bsl(mod, text)
    print(f"[OK] Обработка {name}: {s['action']['kind']} для {s['object']}")
    print(f"     {out / (name + '.xml')}")
    print(f"     {mod}")
    print("Дальше: собери обработку навыком 1c-epf-build (-SourceFile корневой XML, -OutputFile .epf).")


if __name__ == "__main__":
    main()
