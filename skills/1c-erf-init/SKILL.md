---
name: 1c-erf-init
description: Создать пустой внешний отчет 1С (scaffold XML-исходников)
argument-hint: <Name> [Synonym] [SrcDir] [-WithSKD]
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
---

# /erf-init - Создание нового отчета

Генерирует минимальный набор XML-исходников для внешнего отчета 1С: корневой файл метаданных и каталог отчета.

## Usage

```
/erf-init <Name> [Synonym] [SrcDir] [-WithSKD]
```

| Параметр  | Обязательный | По умолчанию | Описание                              |
|-----------|:------------:|--------------|---------------------------------------|
| Name      | да           | -            | Имя отчета (латиница/кириллица)       |
| Synonym   | нет          | = Name       | Синоним (отображаемое имя)            |
| SrcDir    | нет          | `src`        | Каталог исходников относительно CWD   |
| -WithSKD  | нет          | -            | Создать пустую СКД и привязать к MainDataCompositionSchema |

Ключ пишется ровно `-WithSKD` (один дефис, без вариантов `--with-skd`).

## Команда

```powershell
powershell.exe -NoProfile -File skills/1c-erf-init/scripts/init.ps1 -Name "<Name>" [-Synonym "<Synonym>"] [-SrcDir "<SrcDir>"] [-WithSKD]
```

## Что создается с -WithSKD

```
<SrcDir>/<Name>.xml                                              корень отчета, ссылается на схему
<SrcDir>/<Name>/Templates/ОсновнаяСхемаКомпоновкиДанных.xml      метаданные макета - НЕ изменять
<SrcDir>/<Name>/Templates/ОсновнаяСхемаКомпоновкиДанных/Ext/Template.xml   тело схемы (пустая СКД)
```

Схема заполняется только в `Ext/Template.xml`. Файл `Templates/ОсновнаяСхемаКомпоновкиДанных.xml` - это
описание макета в метаданных: если записать в него схему, отчет перестанет собираться. Корневой XML отчета
вручную не правится - привязка схемы уже сделана.

## Дальнейшие шаги

- Заполнить схему (с -WithSKD): `1c-skd-compile` с
  `-OutputPath "<SrcDir>/<Name>/Templates/ОсновнаяСхемаКомпоновкиДанных/Ext/Template.xml"`,
  затем `1c-skd-validate` по тому же пути
- Добавить форму: `/form-add`
- Добавить макет: `/template-add`
- Добавить справку: `/help-add`
- Собрать ERF: `/erf-build`
