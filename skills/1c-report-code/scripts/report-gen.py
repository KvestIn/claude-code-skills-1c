# file: report-gen.py
# Генератор внешнего отчета на СКД из описания JSON: модель описывает источник данных (остатки регистра на дату,
# обороты регистра за период, срез последних регистра сведений, документ или его табличная часть), измерения,
# ресурсы, группировки, отборы и диаграмму, генератор сверяет имена с выгрузкой, сам строит текст запроса (параметр
# виртуальной таблицы, суффиксы ресурсов, отбор по проведенным, реквизиты шапки из табличной части) и собирает отчет
# навыками 1c-erf-init и 1c-skd-compile.
# Без генератора: в отчетах в Остатки передавался период вместо даты, терялась диаграмма, группировка шла не по тем
# полям, нет отбора по проведенным; отсутствующий в регистре вид цены заменялся пустыми выдуманными объектами.
# Запуск: python report-gen.py -SpecPath <spec.json>
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
FOLDERS = {"AccumulationRegister": ("AccumulationRegisters", "РегистрНакопления"),
           "InformationRegister": ("InformationRegisters", "РегистрСведений"),
           "Document": ("Documents", "Документ"), "Catalog": ("Catalogs", "Справочник")}
STD = {"Document": {"Ссылка": "", "Номер": "string", "Дата": "dateTime", "Проведен": "boolean"},
       "Catalog": {"Ссылка": "", "Код": "string", "Наименование": "string"},
       "AccumulationRegister": {"Регистратор": "", "Период": "dateTime"},
       "InformationRegister": {"Период": "dateTime", "Регистратор": ""}}
KIND_DIRS = {"catalogs", "documents", "dataprocessors", "reports", "informationregisters", "accumulationregisters",
             "commonmodules", "enums", "ext", "languages", "roles", "subsystems"}
AGG = {"Сумма", "Количество", "Максимум", "Минимум", "Среднее"}


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


def read_meta(config, full):
    kind, _, name = full.partition(".")
    if kind not in FOLDERS:
        fail(f"'{full}': поддерживаются AccumulationRegister, InformationRegister, Document, Catalog.")
    folder = FOLDERS[kind][0]
    path = Path(config) / folder / f"{name}.xml"
    if not path.is_file():
        have = sorted(p.stem for p in (Path(config) / folder).glob("*.xml"))
        fail(f"Объекта {full} нет в выгрузке. {folder}: {', '.join(have[:100])}{' ...' if len(have) > 100 else ''}")
    obj = next(iter(ET.parse(path).getroot()))
    props = kid(obj, "Properties")

    def fields(el, tags):
        out = {}
        for c in el if el is not None else []:
            if local(c.tag) in tags:
                n = kid(kid(c, "Properties"), "Name").text
                types = [t.text.strip().split(":")[-1] for t in c.iter()
                         if local(t.tag) == "Type" and t.text and t.text.strip()]
                digits = next((t.text for t in c.iter() if local(t.tag) == "Digits" and t.text), None)
                frac = next((t.text for t in c.iter() if local(t.tag) == "FractionDigits" and t.text), "0")
                types = [f"decimal({digits},{frac})" if x == "decimal" and digits else x for x in types]
                out[n] = (types, local(c.tag))
        return out
    ch = kid(obj, "ChildObjects")
    meta = {"kind": kind, "name": name, "ru": FOLDERS[kind][1],
            "fields": fields(ch, {"Attribute", "Dimension", "Resource"}), "tabular": {}}
    for t in ch if ch is not None else []:
        if local(t.tag) == "TabularSection":
            meta["tabular"][kid(kid(t, "Properties"), "Name").text] = fields(kid(t, "ChildObjects"), {"Attribute"})
    rt = kid(props, "RegisterType")
    meta["balance"] = rt is not None and rt.text == "Balance"
    per = kid(props, "InformationRegisterPeriodicity")
    meta["periodic"] = per is not None and per.text != "Nonperiodical"
    return meta


def skd_type(types):
    if not types:
        return None
    t = types[0]
    if t == "string":
        return "string"
    if t == "decimal":
        return "decimal(15,2)"
    if t.startswith("decimal(") or t in ("boolean", "date", "dateTime"):
        return t
    if re.match(r"(Catalog|Document|Enum)Ref\.", t):
        return t
    return None


class Gen:
    def __init__(self, spec):
        self.spec = spec
        self.config = spec["configPath"]

    def resolve(self, meta, path, where, ts=None):
        """Поле источника: имя реквизита/измерения/ресурса или путь через ссылку (Номенклатура.ВидНоменклатуры).
        Для табличной части первым идут ее колонки, затем реквизиты шапки через Ссылка."""
        head, _, rest = path.partition(".")
        pool = dict(meta["tabular"][ts]) if ts else dict(meta["fields"])
        std = STD.get(meta["kind"], {})
        if ts:
            if head not in pool and (head in meta["fields"] or head in std):
                types = meta["fields"].get(head, ([std.get(head)] if std.get(head) else [], ""))[0]
                return f"Ссылка.{path}", self.sub_type(types, rest, where)
            if head not in pool:
                fail(f"{where}: поля '{head}' нет ни в табличной части {ts}, ни в шапке {meta['ru']}.{meta['name']}. "
                     f"Колонки: {', '.join(sorted(pool))}; реквизиты шапки: {', '.join(sorted(meta['fields']))}.")
        elif head not in pool and head not in std:
            fail(f"{where}: поля '{head}' нет у {meta['ru']}.{meta['name']}. Есть: "
                 f"{', '.join(sorted(pool))}. Нет данных - не выдумывай объект или реквизит, сообщи пользователю.")
        types = pool[head][0] if head in pool else ([std[head]] if std.get(head) else [])
        return path, self.sub_type(types, rest, where)

    def sub_type(self, types, rest, where):
        if not rest:
            return skd_type(types)
        ref = next((t for t in types if re.match(r"(Catalog|Document)Ref\.", t)), None)
        if not ref:
            fail(f"{where}: путь через точку возможен только от ссылки на справочник или документ.")
        kind, name = ref.split("Ref.")
        sub = read_meta(self.config, f"{kind}.{name}")
        first, _, more = rest.partition(".")
        if first not in sub["fields"] and first not in STD.get(kind, {}):
            fail(f"{where}: у {kind}.{name} нет реквизита '{first}'. Реквизиты: {', '.join(sorted(sub['fields']))}.")
        types2 = sub["fields"][first][0] if first in sub["fields"] else [STD[kind][first]]
        return self.sub_type(types2, more, where)

    def resource_column(self, meta, name, suffix, ts, label):
        """Колонка источника для ресурса: у виртуальной таблицы регистра накопления - с суффиксом (Сумма -> СуммаОборот,
        КоличествоОстаток), в выражениях тоже (выражение "Сумма - СуммаНДС" по Обороты)."""
        if suffix and meta["kind"] == "AccumulationRegister":
            base = re.sub(r"(Остаток|Оборот|Приход|Расход)$", "", name)
            if base in meta["fields"] and meta["fields"][base][1] == "Resource":
                return name if name != base else base + suffix
            if name in meta["fields"] or name in STD.get(meta["kind"], {}):
                return name
            have = [n for n, (_, tag) in meta["fields"].items() if tag == "Resource"]
            fail(f"resources '{label}': ресурса '{base}' у регистра нет. Ресурсы: {', '.join(have)}.")
        col, _ = self.resolve(meta, name, f"resources '{label}'", ts)
        return col

    def build(self):
        s = self.spec
        src = s.get("source") or fail("В описании нет source.")
        params, table, alias, where_cond, posted = [], "", "Источник", [], False
        ts = None
        if src.get("balance"):
            meta = read_meta(self.config, src["balance"])
            if meta["kind"] != "AccumulationRegister" or not meta["balance"]:
                fail(f"source.balance: {src['balance']} - не регистр накопления остатков. Для оборотного регистра "
                     f"используй source.turnovers.")
            table = f"{meta['ru']}.{meta['name']}.Остатки(&Дата)"
            params.append({"name": "Дата", "title": "На дату", "type": "dateTime"})
            suffix = "Остаток"
        elif src.get("turnovers"):
            meta = read_meta(self.config, src["turnovers"])
            if meta["kind"] != "AccumulationRegister":
                fail("source.turnovers: нужен регистр накопления.")
            table = f"{meta['ru']}.{meta['name']}.Обороты(&НачалоПериода, &КонецПериода)"
            params.append("period")
            suffix = "Оборот"
        elif src.get("sliceLast"):
            meta = read_meta(self.config, src["sliceLast"])
            if meta["kind"] != "InformationRegister" or not meta["periodic"]:
                fail(f"source.sliceLast: {src['sliceLast']} - не периодический регистр сведений.")
            table = f"{meta['ru']}.{meta['name']}.СрезПоследних(&Дата)"
            params.append({"name": "Дата", "title": "На дату", "type": "dateTime"})
            suffix = ""
        elif src.get("document"):
            meta = read_meta(self.config, src["document"])
            if meta["kind"] != "Document":
                fail("source.document: нужен документ.")
            ts = src.get("table")
            if ts and ts not in meta["tabular"]:
                fail(f"source.table: у {src['document']} нет табличной части '{ts}'. Табличные части: "
                     f"{', '.join(sorted(meta['tabular'])) or 'нет'}.")
            table = f"{meta['ru']}.{meta['name']}" + (f".{ts}" if ts else "")
            date = "Ссылка.Дата" if ts else "Дата"
            where_cond.append(f"{alias}.{date} МЕЖДУ &НачалоПериода И &КонецПериода")
            if src.get("posted", True):
                where_cond.append(f"{alias}.{'Ссылка.' if ts else ''}Проведен")
                posted = True
            params.append("period")
            suffix = ""
        else:
            fail("source: укажи balance (остатки регистра на дату), turnovers (обороты за период), sliceLast (срез "
                 "последних регистра сведений) или document (документ, table - его табличная часть).")

        dims = s.get("dimensions") or fail("В описании нет dimensions (поля группировок и отборов).")
        res = s.get("resources") or []
        select, fields, dim_names = [], [], []
        for d in dims:
            name = d["field"] if isinstance(d, dict) else d
            title = d.get("title") if isinstance(d, dict) else None
            path, typ = self.resolve(meta, name, f"dimensions '{name}'", ts)
            al = d.get("alias") if isinstance(d, dict) and d.get("alias") else name.replace(".", "")
            select.append(f"\t{alias}.{path} КАК {al}")
            fields.append(self.field(al, typ, title, "@dimension"))
            dim_names.append(al)
        totals = []
        used_names = set(dim_names)
        default_agg = "Максимум" if src.get("sliceLast") else "Сумма"
        for r in res:
            rd = r if isinstance(r, dict) else {"field": r}
            # name (или alias) - имя колонки в отчете; без него - имя поля (было: два ресурса с полем
            # Сумма и именами Выручка, Прибыль получили одно имя, итоги перепутались)
            out_name = rd.get("name") or rd.get("alias")
            src_field = rd.get("field")
            if not src_field and not rd.get("expr"):
                fail("resources: у ресурса нет field (поле источника) или expr (выражение).")
            label = out_name or src_field
            title = rd.get("title")
            # срез последних - цены и курсы: сумма цен бессмысленна, по умолчанию максимум
            agg = rd.get("agg") or default_agg
            if agg not in AGG:
                fail(f"resources '{label}': agg - {', '.join(sorted(AGG))}.")
            if rd.get("expr"):
                if not label:
                    fail("resources: у выражения укажи name - имя колонки в отчете (например Выручка).")
                parts = []
                for tok in re.split(r"([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё.]*)", rd["expr"]):
                    if re.fullmatch(r"[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё.]*", tok or ""):
                        parts.append(f"{alias}.{self.resource_column(meta, tok, suffix, ts, label)}")
                    else:
                        parts.append(tok)
                al = label
                if al in used_names:
                    fail(f"resources: имя колонки '{al}' уже занято - задай другое name.")
                used_names.add(al)
                select.append(f"	{''.join(parts)} КАК {al}")
                fields.append(self.field(al, rd.get("type", "decimal(15,2)"), title, None))
                totals.append(f"{al}: {agg}")
                continue
            col = self.resource_column(meta, src_field, suffix, ts, label)
            if suffix and meta["kind"] == "AccumulationRegister":
                base = re.sub(r"(Остаток|Оборот|Приход|Расход)$", "", src_field)
                typ = skd_type(meta["fields"][base][0])
            else:
                _, typ = self.resolve(meta, src_field, f"resources '{label}'", ts)
            al = out_name or col.replace(".", "")
            if al in used_names:
                fail(f"resources: имя колонки '{al}' уже занято - задай другое name.")
            used_names.add(al)
            select.append(f"	{alias}.{col} КАК {al}")
            fields.append(self.field(al, typ or "decimal(15,2)", title, None))
            totals.append(f"{al}: {agg}")

        query = "ВЫБРАТЬ РАЗРЕШЕННЫЕ\n" + ",\n".join(select) + f"\nИЗ\n\t{table} КАК {alias}"
        if where_cond:
            query += "\nГДЕ\n\t" + "\n\tИ ".join(where_cond)

        groupings = s.get("groupings", dim_names)
        for g in groupings:
            if g.replace(".", "") not in dim_names:
                fail(f"groupings '{g}': группировать можно только по dimensions: {', '.join(dim_names)}.")
        groupings = [g.replace(".", "") for g in groupings]
        details = s.get("details", False)
        structure = []
        node = None
        for g in reversed(groupings):
            item = {"groupFields": [g]}
            if node:
                item["children"] = [node]
            elif details:
                item["children"] = [{"groupFields": []}]
            node = item
        if node:
            structure.append(node)
        chart = s.get("chart")
        if chart:
            pts, ser = chart.get("points"), chart.get("series")
            val = chart.get("value")
            all_names = dim_names + [t.split(":")[0] for t in totals]
            for k, v in (("points", pts), ("series", ser)):
                if v and v.replace(".", "") not in dim_names:
                    fail(f"chart.{k} '{v}': нужно поле из dimensions: {', '.join(dim_names)}.")
            if not val or val.replace(".", "") not in all_names:
                fail(f"chart.value: нужно поле из resources: {', '.join(t.split(':')[0] for t in totals)}.")
            item = {"type": "chart", "points": {"groupFields": [pts.replace(".", "")]}, "selection": [val]}
            if ser:
                item["series"] = {"groupFields": [ser.replace(".", "")]}
            structure.append(item)

        filters = []
        for f in s.get("filters", []):
            if f.replace(".", "") not in dim_names:
                fail(f"filters '{f}': отбор по полю из dimensions: {', '.join(dim_names)}.")
            filters.append(f"{f.replace('.', '')} = _ @off @user")

        dsl_params = []
        for p in params:
            if p == "period":
                dsl_params.append("Период [Период]: StandardPeriod = ThisMonth @autoDates")
            else:
                dsl_params.append(f"{p['name']} [{p['title']}]: {p['type']}")
        settings = {"selection": ["Auto"], "filter": filters, "dataParameters": "auto", "structure": structure}
        if not filters:
            settings.pop("filter")
        dsl = {"dataSets": [{"name": "Данные", "query": query, "fields": fields}],
               "totalFields": totals, "parameters": dsl_params,
               "settingsVariants": [{"name": "Основной", "presentation": s.get("title", "Основной"),
                                     "settings": settings}]}
        if not totals:
            dsl.pop("totalFields")
        return dsl, posted

    def field(self, name, typ, title, role):
        sh = name + (f": {typ}" if typ else "") + (f" {role}" if role else "")
        if title:
            return {"field": name, "title": title, **({"type": typ} if typ else {}),
                    **({"role": role.strip("@")} if role else {})}
        return sh

    def run(self):
        s = self.spec
        out, name = Path(s["outputDir"]), s["name"]
        if not re.fullmatch(r"[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*", name):
            fail("name - идентификатор отчета без пробелов и точек.")
        dsl, posted = self.build()
        if not (out / f"{name}.xml").is_file():
            run_skill("1c-erf-init/scripts/init.py", "-Name", name, "-Synonym", s.get("title", name), "-SrcDir", out,
                      "-WithSKD")
        tpl = out / name / "Templates" / "ОсновнаяСхемаКомпоновкиДанных" / "Ext" / "Template.xml"
        if not tpl.parent.parent.is_dir():
            fail(f"У отчета {out / name} нет макета ОсновнаяСхемаКомпоновкиДанных: создай отчет заново в пустом каталоге.")
        tmp = out / f".{name}.skd.json"
        tmp.write_text(json.dumps(dsl, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            run_skill("1c-skd-compile/scripts/skd-compile.py", "-DefinitionFile", tmp, "-OutputPath", tpl)
        finally:
            tmp.unlink()
        run_skill("1c-skd-validate/scripts/skd-validate.py", "-TemplatePath", tpl)
        return out / f"{name}.xml", tpl, dsl, posted


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("-SpecPath", required=True)
    a = ap.parse_args()
    spec_path = Path(a.SpecPath).resolve()
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        fail(f"Описание {spec_path} - не JSON: {e}")
    for k in ("configPath", "outputDir", "name", "source"):
        if k not in spec:
            fail(f"В описании нет ключа '{k}'.")

    root = base_dir(spec_path, [(spec.get("configPath"), True), (spec.get("outputDir"), False)])

    def norm(value):
        m = re.match(r"^/([a-zA-Z])(/.*)?$", value)
        if m and sys.platform == "win32":
            value = f"{m.group(1).upper()}:{m.group(2) or '/'}"
        return str(root / value)
    spec["configPath"], spec["outputDir"] = norm(spec["configPath"]), norm(spec["outputDir"])
    cfg, out = Path(spec["configPath"]).resolve(), Path(spec["outputDir"]).resolve()
    if not (cfg / "Configuration.xml").is_file():
        fail(f"В configPath нет Configuration.xml: {cfg}. Укажи каталог XML-выгрузки конфигурации.")
    if out == cfg or (cfg in out.parents and out.relative_to(cfg).parts[0].lower() in KIND_DIRS):
        fail(f"outputDir {out} внутри выгрузки конфигурации: внешний отчет клади в отдельный каталог, например src/.")
    root, tpl, dsl, posted = Gen(spec).run()
    print(f"[OK] Внешний отчет {spec['name']} ({spec.get('title', spec['name'])})")
    print(f"     {root}")
    print(f"     {tpl}")
    print("Запрос:")
    print("\n".join("     " + ln for ln in dsl["dataSets"][0]["query"].split("\n")))
    print("Дальше: собери отчет навыком 1c-epf-build (-SourceFile корневой XML, -OutputFile .erf).")


if __name__ == "__main__":
    main()
