# file: form-code-gen.py
# Генератор программной доработки формы типового объекта в расширении из описания JSON: модель описывает, ЧТО
# добавить (поле, группу, надпись со значением из запроса, таблицу из запроса, кнопку с командой, обработчик
# изменения поля, условное оформление списка, реквизит объекта), генератор сверяет имена с выгрузкой конфигурации
# и формой и пишет код по проверенным приемам:
#   - заимствование формы (1c-cfe-borrow), при необходимости реквизит объекта в расширении (1c-meta-edit);
#   - перехват &После("ПриСозданииНаСервере"): ИзменитьРеквизиты, Элементы.Добавить/Вставить, Команды.Добавить,
#     УстановитьДействие для поля без своего обработчика, УсловноеОформление;
#   - &После на существующем обработчике поля, &Перед("ПередЗаписьюНаСервере") для переноса значения в объект.
# Имени поля, элемента, табличной части или таблицы запроса, которых нет в выгрузке, - отказ со списком имеющихся.
# Замер 15.10.2026: решения задач форм теряли баллы на путях к данным, выдуманных сигнатурах и заимствованных
# формах, которые не загружаются.
# Запуск: python form-code-gen.py -SpecPath <spec.json>
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
STD = {"Document": {"Номер", "Дата", "Ссылка", "Проведен", "ПометкаУдаления"},
       "Catalog": {"Код", "Наименование", "Ссылка", "Родитель", "Владелец", "ПометкаУдаления", "ЭтоГруппа"}}
KIND_DIRS = {"Catalogs", "Documents", "DataProcessors", "Reports", "InformationRegisters", "AccumulationRegisters",
             "CommonModules", "CommonForms", "CommonTemplates", "Enums", "Constants", "ExchangePlans", "Ext", "Languages",
             "Roles", "Subsystems", "DefinedTypes", "CommonPictures", "StyleItems"}
QUERY_TABLES = {"Документ": "Documents", "Справочник": "Catalogs", "РегистрНакопления": "AccumulationRegisters",
                "РегистрСведений": "InformationRegisters", "РегистрБухгалтерии": "AccountingRegisters",
                "Перечисление": "Enums"}
REF_TYPES = {"CatalogRef": ("Catalogs", "СправочникСсылка"), "DocumentRef": ("Documents", "ДокументСсылка"),
             "EnumRef": ("Enums", "ПеречислениеСсылка")}
APPEARANCE = {"ЦветТекста", "ЦветФона", "Шрифт", "Видимость", "Доступность", "ТолькоПросмотр", "Текст"}
FORM_EVENTS = {"ПриСозданииНаСервере": ("OnCreateAtServer", "Отказ, СтандартнаяОбработка"),
               "ПередЗаписьюНаСервере": ("BeforeWriteAtServer", "Отказ, ТекущийОбъект, ПараметрыЗаписи")}
ITEM_TAGS = {"InputField", "UsualGroup", "Pages", "Page", "Table", "LabelDecoration", "LabelField", "Button",
             "CheckBoxField", "PictureDecoration", "PictureField", "ButtonGroup", "Popup", "CommandBar",
             "RadioButtonField", "TextDocumentField", "SpreadSheetDocumentField", "HTMLDocumentField",
             "CalendarField", "ColumnGroup", "ProgressBarField", "TrackBarField", "ChartField", "PlannerField",
             "FormattedDocumentField", "GraphicalSchemaField", "DendrogramField", "PeriodField"}


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


def kids(el, name):
    found = kid(el, name)
    return list(found) if found is not None else []


# --- метаданные --------------------------------------------------------------------------------------------

def read_object(config, kind, name):
    path = Path(config) / KINDS[kind][0] / f"{name}.xml"
    if not path.is_file():
        fail(f"Объекта {kind}.{name} нет в выгрузке ({path}). Сверь имя по выгрузке: синоним и имя различаются.")
    obj = next(iter(ET.parse(path).getroot()))
    children = kid(obj, "ChildObjects")

    def attrs(el):
        out = {}
        for a in el if el is not None else []:
            if local(a.tag) != "Attribute":
                continue
            n = kid(kid(a, "Properties"), "Name")
            types = [t.text.strip().split(":")[-1] for t in a.iter()
                     if local(t.tag) == "Type" and t.text and t.text.strip()]
            out[n.text] = types
        return out
    tss = {}
    forms = []
    for t in children if children is not None else []:
        if local(t.tag) == "TabularSection":
            tss[kid(kid(t, "Properties"), "Name").text] = attrs(kid(t, "ChildObjects"))
        elif local(t.tag) == "Form":
            forms.append(t.text)
    owners_el = kid(kid(obj, "Properties"), "Owners")
    owners = [o.text for o in owners_el if o.text] if owners_el is not None else []
    return {"attributes": attrs(children), "tabular": tss, "std": STD[kind], "forms": forms, "owners": owners}


def read_form(config, kind, name, form):
    path = Path(config) / KINDS[kind][0] / name / "Forms" / form / "Ext" / "Form.xml"
    if not path.is_file():
        fail(f"Формы {form} у {kind}.{name} нет ({path}).")
    root = ET.parse(path).getroot()
    items, children = {}, {"": []}

    def walk(container, parent):
        for el in container:
            if local(el.tag) not in ITEM_TAGS or "name" not in el.attrib:
                continue
            n = el.attrib["name"]
            events = {e.attrib.get("name"): e.text for e in kids(el, "Events")}
            items[n] = {"tag": local(el.tag), "parent": parent, "events": events}
            children[parent].append(n)
            children[n] = []
            sub = kid(el, "ChildItems")
            if sub is not None:
                walk(sub, n)
    walk(kids(root, "ChildItems"), "")
    form_events = {e.attrib.get("name"): e.text for e in kids(root, "Events")}
    attributes = {}
    for a in kids(root, "Attributes"):
        if local(a.tag) != "Attribute":
            continue
        types = [t.text for t in a.iter() if local(t.tag) == "Type" and t.text]
        main_table = next((t.text for t in a.iter() if local(t.tag) == "MainTable" and t.text), None)
        attributes[a.attrib["name"]] = {"types": types, "main": kid(a, "MainAttribute") is not None
                                        and kid(a, "MainAttribute").text == "true", "mainTable": main_table}
    return {"items": items, "children": children, "events": form_events, "attributes": attributes}


# --- построение кода ---------------------------------------------------------------------------------------

def q(s):
    return '"' + str(s).replace('"', '""') + '"'


def call(head, args):
    """Вызов по правилам оформления: два и более параметров - по параметру на строку. Аргумент - строка или
    список строк (многострочное выражение). Возвращает строки без отступа и без завершающей ';'."""
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


def type_expr(config, t):
    m = re.fullmatch(r"string(?:\((\d+)\))?", t)
    if m:
        return call("ОбщегоНазначения.ОписаниеТипаСтрока", [m.group(1)]) if m.group(1) \
            else ['Новый ОписаниеТипов("Строка")']
    m = re.fullmatch(r"(?:number|decimal)\((\d+)(?:,\s*(\d+))?\)", t)
    if m:
        args = [m.group(1)] + ([m.group(2)] if m.group(2) else [])
        return call("ОбщегоНазначения.ОписаниеТипаЧисло", args)
    if t in ("date", "dateTime"):
        return [f"ОбщегоНазначения.ОписаниеТипаДата(ЧастиДаты.{'Дата' if t == 'date' else 'ДатаВремя'})"]
    if t == "boolean":
        return ['Новый ОписаниеТипов("Булево")']
    m = re.fullmatch(r"(CatalogRef|DocumentRef|EnumRef)\.(\w+)", t)
    if m:
        folder, ru = REF_TYPES[m.group(1)]
        if not (Path(config) / folder / f"{m.group(2)}.xml").is_file():
            fail(f"Тип {t}: объекта {m.group(2)} нет в выгрузке ({folder}).")
        return [f'Новый ОписаниеТипов("{ru}.{m.group(2)}")']
    if t == "ValueTable":
        return ['Новый ОписаниеТипов("ТаблицаЗначений")']
    fail(f"Тип '{t}' не поддержан: string, string(N), number(N,M), date, dateTime, boolean, CatalogRef.X, "
         f"DocumentRef.X, EnumRef.X.")


def humanize(name):
    """КомментарийДляСклада -> Комментарий для склада."""
    words = re.findall(r"[A-ZА-ЯЁ][a-zа-яё0-9]*|[a-zа-яё0-9]+", name)
    return " ".join([words[0]] + [w.lower() for w in words[1:]]) if words else name


def bsl_value(v):
    """Литерал BSL из значения JSON; строка - строковый литерал."""
    if isinstance(v, bool):
        return "Истина" if v else "Ложь"
    if v is None:
        return "Неопределено"
    if isinstance(v, (int, float)):
        return str(v)
    return q(v)


def meta_type(t):
    m = re.fullmatch(r"string(?:\((\d+)\))?", t)
    if m:
        return f"Строка({m.group(1) or 0})"
    m = re.fullmatch(r"(?:number|decimal)\((\d+)(?:,\s*(\d+))?\)", t)
    if m:
        return f"Число({m.group(1)},{m.group(2) or 0})"
    return {"date": "Дата", "dateTime": "ДатаВремя", "boolean": "Булево"}.get(t, t)


def query_lines(config, text, params, ctx):
    """Текст запроса: таблицы сверяются с выгрузкой, параметры - с описанием; РАЗРЕШЕННЫЕ в первом пакете."""
    for kind_ru, name in re.findall(r"(Документ|Справочник|РегистрНакопления|РегистрСведений|РегистрБухгалтерии|"
                                    r"Перечисление)\.([\wА-Яа-яЁё]+)", text):
        folder = QUERY_TABLES[kind_ru]
        if not (Path(config) / folder / f"{name}.xml").is_file():
            have = sorted(p.stem for p in (Path(config) / folder).glob("*.xml"))
            fail(f"{ctx}: в запросе таблица {kind_ru}.{name}, такого объекта нет. Объекты {folder}: "
                 f"{', '.join(have[:80])}{' ...' if len(have) > 80 else ''}")
    used = set(re.findall(r"&([\wА-Яа-яЁё]+)", text))
    if used != set(params):
        fail(f"{ctx}: параметры запроса {sorted(used)} не совпадают с params {sorted(params)}.")
    text = re.sub(r"^\s*ВЫБРАТЬ(?!\s+РАЗРЕШЕННЫЕ)", "ВЫБРАТЬ РАЗРЕШЕННЫЕ", text.strip(), count=1)
    rows = text.replace('"', '""').split("\n")
    out = ["Запрос = Новый Запрос;", "Запрос.Текст =", '\t"' + rows[0]]
    out += ["\t|" + r for r in rows[1:]]
    out[-1] += '";'
    for p, v in params.items():
        out += stmt(call("Запрос.УстановитьПараметр", [q(p), v]))
    return out


class Gen:
    def __init__(self, spec):
        self.spec = spec
        self.config = spec["configPath"]
        self.kind, _, self.name = spec["object"].partition(".")
        if self.kind not in KINDS:
            fail("object - 'Document.<Имя>' или 'Catalog.<Имя>'.")
        self.meta = read_object(self.config, self.kind, self.name)
        if spec["form"] not in self.meta["forms"]:
            fail(f"Формы '{spec['form']}' у {spec['object']} нет. Формы: {', '.join(self.meta['forms'])}.")
        self.form = read_form(self.config, self.kind, self.name, spec["form"])
        self.is_object_form = "Объект" in self.form["attributes"]
        self.prefix = ""
        self.new_attrs = {}      # реквизиты объекта в расширении: имя из описания -> полное имя
        self.add_attrs = []      # строки массива ИзменитьРеквизиты
        self.create = []         # строки ПриСозданииНаСервере: элементы
        self.after_create = []   # строки ПриСозданииНаСервере после элементов: действия, оформление
        self.loads = []          # значения реквизитов формы из объекта
        self.calls = []          # заполнение надписей и таблиц
        self.before_write = []
        self.server_procs = []
        self.client_procs = []
        self.command_procs = []
        self.field_handlers = {}  # поле -> {"server": [...], "client": [...]}
        self.new_items = {}       # полное имя нового элемента -> вид

    # имена и позиции
    def full(self, n):
        return n if n.startswith(self.prefix) else self.prefix + n

    def item_exists(self, n):
        return n in self.form["items"] or n in self.new_items

    def check_item(self, n, what):
        if not self.item_exists(n):
            near = [i for i in self.form["items"] if self.form["items"][i]["tag"] != "Button"]
            fail(f"{what}: элемента '{n}' на форме {self.spec['form']} нет. Элементы формы: "
                 f"{', '.join(near[:120])}{' ...' if len(near) > 120 else ''}")

    def place(self, el, n, type_ru):
        ch = self.form["children"]
        if el.get("after"):
            after = self.resolve(el["after"])
            self.check_item(after, f"'{el['name']}'.after")
            parent = self.parent_of(after)
            idx = ch[parent].index(after)
            nxt = ch[parent][idx + 1] if idx + 1 < len(ch[parent]) else None
            ch[parent].insert(idx + 1, n)
        elif el.get("into"):
            parent = self.resolve(el["into"])
            self.check_item(parent, f"'{el['name']}'.into")
            nxt = None
            ch[parent].append(n)
        else:
            fail(f"У элемента '{el['name']}' нет места: укажи after (имя элемента, после которого) или into (группа).")
        ch[n] = []
        self.parents[n] = parent
        parent_expr = "ЭтотОбъект" if parent == "" else f"Элементы.{parent}"
        if nxt:
            return call("Элементы.Вставить", [q(n), f'Тип("{type_ru}")', parent_expr, f"Элементы.{nxt}"])
        return call("Элементы.Добавить", [q(n), f'Тип("{type_ru}")', parent_expr])

    def parent_of(self, n):
        if n in self.parents:
            return self.parents[n]
        return self.form["items"][n]["parent"]

    def resolve(self, n):
        return self.full(n) if self.full(n) in self.new_items else n

    def check_data(self, path, what):
        """Путь Объект.Реквизит: реквизит объекта, стандартный или добавленный описанием."""
        if not self.is_object_form:
            fail(f"{what}: у формы {self.spec['form']} нет реквизита Объект - это не форма объекта.")
        head, _, attr = path.partition(".")
        if head != "Объект" or not attr or "." in attr:
            fail(f"{what}: путь к данным - 'Объект.<Реквизит>', получено '{path}'.")
        if attr not in self.meta["attributes"] and attr not in self.meta["std"] and attr not in self.new_attrs.values():
            fail(f"{what}: у {self.spec['object']} нет реквизита '{attr}'. Реквизиты: "
                 f"{', '.join(sorted(self.meta['attributes']))}. Нужен новый - опиши его в objectAttributes.")

    def param_value(self, v, what):
        if isinstance(v, str) and v.startswith("Объект."):
            self.check_data(v, what)
        return v

    # элементы
    def add_group(self, el):
        n = self.full(el["name"])
        parent_tag = None
        lines = self.place(el, n, "ГруппаФормы")
        parent = self.parents[n]
        if parent:
            parent_tag = self.form["items"].get(parent, {}).get("tag") or self.new_items.get(parent)
        self.new_items[n] = "Page" if parent_tag == "Pages" else "UsualGroup"
        out = stmt(lines, "Группа")
        if parent_tag == "Pages":
            pairs = [("Группа.Вид", "ВидГруппыФормы.Страница")]
        else:
            pairs = [("Группа.Вид", "ВидГруппыФормы.ОбычнаяГруппа"),
                     ("Группа.Группировка", "ГруппировкаПодчиненныхЭлементовФормы."
                      + ("Горизонтальная" if el.get("horizontal") else "Вертикальная"))]
        if el.get("title"):
            pairs += [("Группа.Заголовок", q(el["title"])), ("Группа.ОтображатьЗаголовок", "Истина")]
        elif parent_tag != "Pages":
            pairs += [("Группа.Отображение", "ОтображениеОбычнойГруппы.Нет"), ("Группа.ОтображатьЗаголовок", "Ложь")]
        self.create += out + aligned(pairs) + [""]

    def add_field(self, el):
        n = self.full(el["name"])
        if el.get("objectAttribute"):
            if el["objectAttribute"] not in self.new_attrs:
                fail(f"Поле '{el['name']}': objectAttribute '{el['objectAttribute']}' нет в objectAttributes.")
            data = "Объект." + self.new_attrs[el["objectAttribute"]]
        elif el.get("data"):
            data = el["data"]
            self.check_data(data, f"Поле '{el['name']}'")
        elif el.get("type"):
            data = n
            args = [q(n), type_expr(self.config, el["type"])]
            if el.get("title"):
                args += ["", q(el["title"])]
            self.add_attrs += stmt(call("ДобавляемыеРеквизиты.Добавить", [call("Новый РеквизитФормы", args)]))
            for key, what in (("load", "загрузка"), ("save", "запись")):
                if el.get(key):
                    self.check_data("Объект." + el[key], f"Поле '{el['name']}'.{key} ({what})")
            if el.get("load"):
                # реквизит, созданный ИзменитьРеквизиты, компилятору неизвестен: обращение по имени дает
                # "Переменная не определена" или молча заводит локальную переменную (проверка 15.10.2026)
                self.loads.append(f"ЭтотОбъект[{q(n)}] = Объект.{el['load']};")
            if el.get("save"):
                self.before_write.append(f"ТекущийОбъект.{el['save']} = ЭтотОбъект[{q(n)}];")
        else:
            fail(f"Поле '{el['name']}': укажи data (Объект.Реквизит), objectAttribute (новый реквизит объекта) или "
                 f"type (реквизит формы, создаваемый в коде).")
        lines = self.place(el, n, "ПолеФормы")
        self.new_items[n] = "InputField"
        pairs = [("Поле.Вид", "ВидПоляФормы." + ("ПолеФлажка" if el.get("type") == "boolean" else "ПолеВвода")),
                 ("Поле.ПутьКДанным", q(data))]
        if el.get("title") and not el.get("type"):
            pairs.append(("Поле.Заголовок", q(el["title"])))
        if el.get("multiline"):
            pairs.append(("Поле.МногострочныйРежим", "Истина"))
        if el.get("readOnly"):
            pairs.append(("Поле.ТолькоПросмотр", "Истина"))
        self.create += stmt(lines, "Поле") + aligned(pairs) + [""]

    def add_label(self, el):
        n = self.full(el["name"])
        if not self.is_object_form:
            fail(f"Надпись '{el['name']}' со значением из запроса - только на форме объекта.")
        text = el.get("query")
        if not text or not re.search(r"\bКАК\s+Значение\b", text):
            fail(f"Надпись '{el['name']}': query должен выбирать одно поле 'КАК Значение'.")
        params = {k: self.param_value(v, f"Надпись '{el['name']}'.params") for k, v in el.get("params", {}).items()}
        self.create += stmt(self.place(el, n, "ДекорацияФормы"), "Надпись") + ["Надпись.Вид = ВидДекорацииФормы.Надпись;", ""]
        self.new_items[n] = "LabelDecoration"
        proc = f"{self.prefix}Обновить{el['name']}"
        default = el.get("default", 0)
        default = bsl_value(default)
        fmt = el.get("format", "ЧДЦ=2; ЧН=0")
        body = ["&НаСервере", f"Процедура {proc}()", ""]
        guards = [v for v in params.values() if isinstance(v, str) and v.startswith("Объект.")]
        if guards:
            body += indent([f"Если НЕ ЗначениеЗаполнено({guards[0]}) Тогда", f'\tЭлементы.{n}.Заголовок = "";',
                            "\tВозврат;", "КонецЕсли;", ""])
        body += indent(query_lines(self.config, text, params, f"Надпись '{el['name']}'"))
        body += indent(["", "РезультатЗапроса = Запрос.Выполнить();", "Выборка = РезультатЗапроса.Выбрать();",
                        "", f"Значение = {default};", "Если Выборка.Следующий() И Выборка.Значение <> NULL Тогда",
                        "\tЗначение = Выборка.Значение;", "КонецЕсли;", ""])
        body += indent(stmt(call("СтрШаблон", [q(el.get("template", "%1")), f"Формат(Значение, {q(fmt)})"]),
                            f"Элементы.{n}.Заголовок"))
        body += ["", "КонецПроцедуры"]
        self.server_procs.append(body)
        self.calls.append(f"{proc}();")
        for f in el.get("refreshOn", []):
            self.check_item(f, f"Надпись '{el['name']}'.refreshOn")
            self.field_handlers.setdefault(f, {"server": [], "client": []})["server"].append(f"{proc}();")

    def add_table(self, el):
        n = self.full(el["name"])
        if not self.is_object_form:
            fail(f"Таблица '{el['name']}' из запроса - только на форме объекта.")
        cols = el.get("columns") or fail(f"Таблица '{el['name']}': нет columns.")
        text = el.get("query", "")
        for c in cols:
            if not re.search(rf"\bКАК\s+{re.escape(c['name'])}\b", text):
                fail(f"Таблица '{el['name']}': в запросе нет поля 'КАК {c['name']}' для колонки {c['name']}.")
        params = {k: self.param_value(v, f"Таблица '{el['name']}'.params") for k, v in el.get("params", {}).items()}
        args = [q(n), type_expr(self.config, "ValueTable")] + (["", q(el["title"])] if el.get("title") else [])
        self.add_attrs += stmt(call("ДобавляемыеРеквизиты.Добавить", [call("Новый РеквизитФормы", args)]))
        for c in cols:
            cargs = [q(c["name"]), type_expr(self.config, c.get("type", "string")), q(n)]
            if c.get("title"):
                cargs.append(q(c["title"]))
            self.add_attrs += stmt(call("ДобавляемыеРеквизиты.Добавить", [call("Новый РеквизитФормы", cargs)]))
        self.create += stmt(self.place(el, n, "ТаблицаФормы"), "Таблица")
        self.new_items[n] = "Table"
        pairs = [("Таблица.ПутьКДанным", q(n)), ("Таблица.ТолькоПросмотр", "Истина"),
                 ("Таблица.ИзменятьСоставСтрок", "Ложь")]
        if el.get("title"):
            pairs += [("Таблица.Заголовок", q(el["title"])), ("Таблица.ПоложениеЗаголовка",
                                                               "ПоложениеЗаголовкаЭлементаФормы.Верх")]
        if el.get("height"):
            pairs.append(("Таблица.Высота", str(el["height"])))
        self.create += aligned(pairs) + [""]
        for c in cols:
            self.create += stmt(call("Элементы.Добавить", [q(n + c["name"]), 'Тип("ПолеФормы")', "Таблица"]), "Колонка")
            self.create += aligned([("Колонка.Вид", "ВидПоляФормы.ПолеВвода"),
                                    ("Колонка.ПутьКДанным", q(f"{n}.{c['name']}"))]) + [""]
        proc = f"{self.prefix}Заполнить{el['name']}"
        body = ["&НаСервере", f"Процедура {proc}()", ""]
        body += indent(query_lines(self.config, text, params, f"Таблица '{el['name']}'"))
        body += indent(["", "РезультатЗапроса = Запрос.Выполнить();",
                        "ЗначениеВРеквизитФормы(", "\tРезультатЗапроса.Выгрузить(),", f"\t{q(n)}", ");"])
        body += ["", "КонецПроцедуры"]
        self.server_procs.append(body)
        self.calls.append(f"{proc}();")
        for f in el.get("refreshOn", []):
            self.check_item(f, f"Таблица '{el['name']}'.refreshOn")
            self.field_handlers.setdefault(f, {"server": [], "client": []})["server"].append(f"{proc}();")

    def add_button(self, el):
        n = self.full(el["name"])
        self.create += stmt(call("Команды.Добавить", [q(n)]), "Команда")
        self.create += aligned([("Команда.Заголовок", q(el["title"])), ("Команда.Действие", q(n))]) + [""]
        bar = "КоманднаяПанель"
        if el.get("commandBar"):
            self.check_item(el["commandBar"], f"Кнопка '{el['name']}'.commandBar")
            if self.form["items"].get(el["commandBar"], {}).get("tag") != "Table":
                fail(f"Кнопка '{el['name']}': commandBar - имя таблицы формы, '{el['commandBar']}' не таблица.")
            bar = f"Элементы.{el['commandBar']}.КоманднаяПанель"
        self.create += stmt(call("Элементы.Добавить", [q(n), 'Тип("КнопкаФормы")', bar]), "Кнопка")
        self.create += [f"Кнопка.ИмяКоманды = {q(n)};", ""]
        self.new_items[n] = "Button"
        client = []
        if el.get("recalc"):
            client = self.recalc_lines(el["recalc"], el["name"])
        if el.get("body"):
            if el.get("server"):
                srv = f"{n}НаСервере"
                self.server_procs.append(["&НаСервере", f"Процедура {srv}()", ""] + indent(el["body"])
                                         + ["", "КонецПроцедуры"])
                client.append(f"{srv}();")
            else:
                client += el["body"]
        if not client:
            fail(f"Кнопка '{el['name']}': укажи recalc или body (строки кода обработчика).")
        self.command_procs.append(["&НаКлиенте", f"Процедура {n}(Команда)", ""] + indent(client)
                                  + ["", "КонецПроцедуры"])

    def recalc_lines(self, r, what):
        ts = r.get("table")
        if ts not in self.meta["tabular"]:
            fail(f"Кнопка '{what}': табличной части '{ts}' нет. Табличные части: "
                 f"{', '.join(sorted(self.meta['tabular'])) or 'нет'}.")
        cols = self.meta["tabular"][ts]
        idents = re.findall(r"[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*", r["expr"])
        for c in [r["set"]] + idents:
            if c not in cols:
                fail(f"Кнопка '{what}': колонки '{c}' в {ts} нет. Колонки: {', '.join(sorted(cols))}.")
        expr = re.sub(r"([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*)", r"СтрокаТаблицы.\1", r["expr"])
        out = [f"Для Каждого СтрокаТаблицы Из Объект.{ts} Цикл", f"\tСтрокаТаблицы.{r['set']} = {expr};",
               "КонецЦикла;", ""]
        if r.get("total"):
            self.check_data("Объект." + r["total"], f"Кнопка '{what}'.total")
            out += [f'Объект.{r["total"]} = Объект.{ts}.Итог("{r["set"]}");']
        out.append("Модифицированность = Истина;")
        return out

    def add_on_change(self, oc):
        f = oc["field"]
        self.check_item(f, "onChange.field")
        h = self.field_handlers.setdefault(f, {"server": [], "client": []})
        if oc.get("clearIfOwnerDiffers"):
            c = oc["clearIfOwnerDiffers"]
            target, owner_field = c["field"], c.get("owner", "Владелец")
            self.check_data("Объект." + target, "onChange.clearIfOwnerDiffers.field")
            ref = next((t for t in self.meta["attributes"].get(target, []) if t.startswith("CatalogRef.")), None)
            if not ref:
                fail(f"onChange: '{target}' не ссылка на справочник - владельца проверить нельзя.")
            cat = ref.split(".", 1)[1]
            sub = read_object(self.config, "Catalog", cat)
            if owner_field == "Владелец" and not sub["owners"]:
                fail(f"У справочника {cat} нет владельцев. Реквизиты: {', '.join(sorted(sub['attributes']))} - "
                     f"укажи owner (реквизит справочника со ссылкой на контрагента).")
            if owner_field != "Владелец" and owner_field not in sub["attributes"]:
                fail(f"У справочника {cat} нет реквизита '{owner_field}'. Реквизиты: "
                     f"{', '.join(sorted(sub['attributes']))}.")
            source = c.get("value", f"Объект.{f}")
            self.param_value(source, "onChange.clearIfOwnerDiffers.value")
            h["server"] += [f"Если ЗначениеЗаполнено(Объект.{target}) Тогда",
                            *indent(stmt(call("ОбщегоНазначения.ЗначениеРеквизитаОбъекта",
                                              [f"Объект.{target}", q(owner_field)]), f"Владелец{target}")),
                            f"\tЕсли Владелец{target} <> {source} Тогда",
                            f"\t\tОбъект.{target} = Справочники.{cat}.ПустаяСсылка();",
                            "\tКонецЕсли;", "КонецЕсли;"]
        if oc.get("body"):
            h["server" if oc.get("server", True) else "client"] += oc["body"]

    def add_appearance(self, ap):
        lst = ap.get("list", "Список")
        attr = self.form["attributes"].get(lst)
        if not attr or not any("DynamicList" in t for t in attr["types"]):
            fail(f"Условное оформление: на форме нет динамического списка '{lst}'. Реквизиты формы: "
                 f"{', '.join(self.form['attributes'])}.")
        flt = ap.get("filter") or fail("Условное оформление: нет filter.")
        field = flt["field"]
        if attr["mainTable"] and field.split(".")[0] not in set(self.meta["attributes"]) | self.meta["std"]:
            fail(f"Условное оформление: поля '{field}' у {self.spec['object']} нет. Реквизиты: "
                 f"{', '.join(sorted(self.meta['attributes']))}.")
        value = flt.get("value")
        value = value if isinstance(value, str) else bsl_value(value)
        for k in ap.get("set", {}):
            if k not in APPEARANCE:
                fail(f"Условное оформление: параметр '{k}' не поддержан: {', '.join(sorted(APPEARANCE))}.")
        lines = ["ЭлементОформления = УсловноеОформление.Элементы.Добавить();", ""]
        lines += stmt(call("ЭлементОформления.Отбор.Элементы.Добавить", ['Тип("ЭлементОтбораКомпоновкиДанных")']),
                      "ЭлементОтбора")
        lines += aligned([("ЭлементОтбора.ЛевоеЗначение", f'Новый ПолеКомпоновкиДанных("{lst}.{field}")'),
                          ("ЭлементОтбора.ВидСравнения", "ВидСравненияКомпоновкиДанных." + flt.get("compare", "Равно")),
                          ("ЭлементОтбора.ПравоеЗначение", str(value))]) + [""]
        for k, v in ap.get("set", {}).items():
            lines += stmt(call("ЭлементОформления.Оформление.УстановитьЗначениеПараметра", [q(k), v])) + [""]
        for f in ap.get("fields", []):
            self.check_item(f, "Условное оформление.fields")
            lines += ["ПолеОформления = ЭлементОформления.Поля.Элементы.Добавить();",
                      f'ПолеОформления.Поле = Новый ПолеКомпоновкиДанных("{f}");', ""]
        self.after_create += lines

    # события
    def form_event(self, ru):
        """Имя базового обработчика события формы или None (тогда событие добавляется в Form.xml)."""
        return self.form["events"].get(FORM_EVENTS[ru][0])

    def build_field_handlers(self):
        for f, h in self.field_handlers.items():
            base = self.form["items"].get(f, {}).get("events", {}).get("OnChange")
            client = list(h["client"])
            if h["server"]:
                srv = f"{self.prefix}{f}ПриИзмененииНаСервере"
                self.server_procs.append(["&НаСервере", f"Процедура {srv}()", ""] + indent(h["server"])
                                         + ["", "КонецПроцедуры"])
                client.append(f"{srv}();")
            if base:
                head = ["&НаКлиенте", f'&После("{base}")', f"Процедура {self.prefix}{base}(Элемент)"]
            else:
                proc = f"{self.prefix}{f}ПриИзменении"
                self.after_create += stmt(call(f"Элементы.{f}.УстановитьДействие", ['"ПриИзменении"', q(proc)])) + [""]
                head = ["&НаКлиенте", f"Процедура {proc}(Элемент)"]
            self.client_procs.append(head + [""] + indent(client) + ["", "КонецПроцедуры"])

    def module(self):
        self.build_field_handlers()
        on_create = []
        if self.add_attrs:
            on_create += ["ДобавляемыеРеквизиты = Новый Массив;"] + self.add_attrs + \
                         ["ИзменитьРеквизиты(ДобавляемыеРеквизиты);", ""]
        on_create += self.create + self.after_create
        for block in (self.loads, self.calls):
            if block:
                on_create += block + [""]
        while on_create and not on_create[-1]:
            on_create.pop()
        events, xml_events = [], []
        for ru, lines in (("ПриСозданииНаСервере", on_create), ("ПередЗаписьюНаСервере", self.before_write)):
            if not lines:
                continue
            base = self.form_event(ru)
            proc = f"{self.prefix}{ru}"
            params = FORM_EVENTS[ru][1]
            if base:
                anno = "После" if ru == "ПриСозданииНаСервере" else "Перед"
                head = ["&НаСервере", f'&{anno}("{base}")', f"Процедура {proc}({params})"]
            else:
                head = ["&НаСервере", f"Процедура {proc}({params})"]
                xml_events.append({"name": FORM_EVENTS[ru][0], "handler": proc})
            events.append(head + [""] + indent(lines) + ["", "КонецПроцедуры"])
        regions = [("ОбработчикиСобытийФормы", events), ("ОбработчикиСобытийЭлементовШапкиФормы", self.client_procs),
                   ("ОбработчикиКомандФормы", self.command_procs), ("СлужебныеПроцедурыИФункции", self.server_procs)]
        out = []
        for region, procs in regions:
            if procs:
                out += [f"#Область {region}", ""] + "\n\n".join("\n".join(p) for p in procs).split("\n") + \
                       ["", "#КонецОбласти", ""]
        return "\n".join(out), xml_events

    def run(self):
        spec = self.spec
        ext = Path(spec["extension"]["path"])
        folder = KINDS[self.kind][0]
        if not (ext / "Configuration.xml").is_file():
            # путь к каталогу, где лежат расширения, а не к самому расширению: новое пустое расширение рядом с
            # существующими загружается второй раз (задача ext-2 15.10.2026, лишние Configuration.xml, Languages, Roles)
            if ext.is_dir() and any(ext.iterdir()):
                found = sorted(str(c.parent) for c in ext.glob("*/Configuration.xml")) +                     sorted(str(c.parent) for c in ext.glob("*/*/Configuration.xml"))
                fail(f"extension.path {ext} - не каталог расширения (в нем нет Configuration.xml), и он не пустой. "
                     f"Расширения внутри: {', '.join(found) or 'нет'}. Укажи каталог существующего расширения или "
                     f"новый пустой каталог.")
            run_skill("1c-cfe-init/scripts/cfe-init.py", "-Name", spec["extension"]["name"], "-OutputDir", ext,
                      "-ConfigPath", self.config, "-NoRole")
        m = re.search(r"<NamePrefix>([^<]*)</NamePrefix>", (ext / "Configuration.xml").read_text(encoding="utf-8-sig"))
        self.prefix = m.group(1) if m and m.group(1) else spec["extension"]["name"].split("_")[0] + "_"
        self.parents = {}
        for a in spec.get("objectAttributes", []):
            self.new_attrs[a["name"]] = self.full(a["name"])
        # проверка описания до записи файлов: отказ не оставляет расширение наполовину измененным
        for el in spec.get("elements", []):
            getattr(self, {"group": "add_group", "field": "add_field", "label": "add_label", "table": "add_table",
                           "button": "add_button"}.get(el.get("kind"), "bad_kind"))(el)
        for oc in spec.get("onChange", []):
            self.add_on_change(oc)
        for ap in spec.get("appearance", []):
            self.add_appearance(ap)
        for n in self.new_items:
            if n in self.form["items"]:
                fail(f"Элемент '{n}' уже есть на форме - выбери другое имя.")
        text, xml_events = self.module()
        if not text:
            fail("Описание пустое: нет elements, onChange, appearance.")

        form_dir = ext / folder / self.name / "Forms" / spec["form"]
        mod = form_dir / "Ext" / "Form" / "Module.bsl"
        if mod.is_file() and mod.read_text(encoding="utf-8-sig").strip():
            fail(f"Модуль заимствованной формы уже не пустой ({mod}): генератор пишет модуль целиком и чужой код не "
                 f"перезаписывает. Допиши новое вручную по образцу из навыка 1c-form-code.")
        if not (ext / folder / self.name / "Forms" / f"{spec['form']}.xml").is_file():
            # связь параметров выбора с путем Объект.* (форма контрагента Демо БСП: Отбор.Владелец = Объект.Ref)
            # без заимствованного основного реквизита не загружается: "Неверный путь к полю - Объект.Ref"
            base_xml = Path(self.config) / folder / self.name / "Forms" / spec["form"] / "Ext" / "Form.xml"
            main_attr = ["-BorrowMainAttribute"] if re.search(
                r"<xr:DataPath[^>]*>Объект\.", base_xml.read_text(encoding="utf-8-sig")) else []
            run_skill("1c-cfe-borrow/scripts/cfe-borrow.py", "-ExtensionPath", ext, "-ConfigPath", self.config,
                      "-Object", f"{self.kind}.{self.name}.Form.{spec['form']}", *main_attr)
        for a in spec.get("objectAttributes", []):
            obj_xml = ext / folder / f"{self.name}.xml"
            if f"<Name>{self.new_attrs[a['name']]}</Name>" not in obj_xml.read_text(encoding="utf-8-sig"):
                run_skill("1c-meta-edit/scripts/meta-edit.py", "-ObjectPath", obj_xml, "-Operation", "add-attribute",
                          "-Value", f"{self.new_attrs[a['name']]}: {meta_type(a['type'])}")
                # без заголовка навык строит синоним из полного имени с префиксом ("Custom extension_комментарий
                # для склада", задача ext-1 15.10.2026) - синоним задается всегда, по умолчанию из имени без префикса
                title = a.get("title") or humanize(a["name"])
                run_skill("1c-meta-edit/scripts/meta-edit.py", "-ObjectPath", obj_xml, "-Operation",
                          "modify-attribute", "-Value", f"{self.new_attrs[a['name']]}: synonym={title}")
        if xml_events:
            tmp = ext / f".{spec['form']}.events.json"
            tmp.write_text(json.dumps({"formEvents": xml_events}, ensure_ascii=False), encoding="utf-8")
            try:
                run_skill("1c-form-edit/scripts/form-edit.py", "-FormPath", form_dir / "Ext" / "Form.xml",
                          "-JsonPath", tmp)
            finally:
                tmp.unlink()
        write_bsl(mod, text)
        return mod, xml_events

    def bad_kind(self, el):
        fail(f"Элемент '{el.get('name')}': kind '{el.get('kind')}' не поддержан: group, field, label, table, button.")


def write_bsl(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(l if l.strip() else "" for l in text.split("\n"))
    path.write_bytes(("﻿" + text.replace("\n", "\r\n")).encode("utf-8"))


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("-SpecPath", required=True)
    a = ap.parse_args()
    spec_path = Path(a.SpecPath).resolve()
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        fail(f"Описание {spec_path} - не JSON: {e}")
    for k in ("configPath", "object", "form", "extension"):
        if k not in spec:
            fail(f"В описании нет ключа '{k}'.")
    base = spec_path.parent

    def norm(value):
        m = re.match(r"^/([a-zA-Z])(/.*)?$", value)
        if m and sys.platform == "win32":
            value = f"{m.group(1).upper()}:{m.group(2) or '/'}"
        return str(base / value)
    spec["configPath"] = norm(spec["configPath"])
    spec["extension"]["path"] = norm(spec["extension"]["path"])
    if not (Path(spec["configPath"]) / "Configuration.xml").is_file():
        fail(f"В configPath нет Configuration.xml: {spec['configPath']}. Укажи каталог XML-выгрузки конфигурации.")
    cfg, ext = Path(spec["configPath"]).resolve(), Path(spec["extension"]["path"]).resolve()
    # расширение в корне выгрузки или в каталогах ее объектов читается как часть конфигурации; в Windows путь "ext"
    # совпадает с каталогом Ext выгрузки
    if ext == cfg or (cfg in ext.parents and ext.relative_to(cfg).parts[0].lower() in {d.lower() for d in KIND_DIRS}):
        fail(f"extension.path {ext} внутри выгрузки конфигурации: расширение клади в отдельный каталог, например "
             f"cfe/<Имя> рядом с выгрузкой.")
    mod, xml_events = Gen(spec).run()
    print(f"[OK] Доработка формы {spec['object']}.{spec['form']} в расширении {spec['extension']['name']}")
    print(f"     {mod}")
    if xml_events:
        print(f"     события добавлены в Form.xml: {', '.join(e['name'] for e in xml_events)}")
    print("Дальше: проверь расширение (загрузка и синтаксический контроль), код руками не переписывай - "
          "меняй описание и запускай генератор заново на чистом модуле.")


if __name__ == "__main__":
    main()
