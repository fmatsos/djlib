"""A single reusable, static, serverless HTML table renderer shared by every
`djlib * export --format html` command (catalogue/duplicates/stats). Built as
a plain Python string rather than a Jinja template -- there is exactly one
consumer-facing shape (title + sortable/filterable table) needed here, not
the templated multi-file layout `report/generator.py` owns for its own
interactive review workflow, so a second template directory would be a
speculative abstraction for a single f-string's worth of markup. Fully
self-contained: no external resources, opens correctly via `file://` alone.
"""

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    key: str
    label: str


def _script_safe(json_text: str) -> str:
    """Defangs a literal `</` inside JSON text so it cannot prematurely close
    the `<script type="application/json">` block it is inlined into -- the
    same one-line trick as `report/generator.py::_script_safe`, kept as a
    local copy here rather than importing a private helper across modules.
    """
    return json_text.replace('</', '<\\/')


_PAGE_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: sans-serif; margin: 2rem; }}
  #search {{ padding: 0.4rem; width: 100%; max-width: 24rem; margin-bottom: 0.75rem; }}
  #count {{ margin-bottom: 0.5rem; color: #555; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 0.3rem 0.6rem; text-align: left; }}
  th {{ cursor: pointer; background: #f0f0f0; user-select: none; }}
  tr.hidden {{ display: none; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p>Generated at {generated_at}</p>
<input id="search" type="text" placeholder="Filter rows...">
<p id="count"></p>
<table id="data-table">
<thead><tr>{header_cells}</tr></thead>
<tbody>{body_rows}</tbody>
</table>
<script type="application/json" id="row-data">{rows_json}</script>
<script>{app_js}</script>
</body>
</html>
'''

_APP_JS = '''(function () {
  var table = document.getElementById('data-table');
  var tbody = table.tBodies[0];
  var searchBox = document.getElementById('search');
  var counter = document.getElementById('count');
  var rows = Array.prototype.slice.call(tbody.rows);

  function updateCount() {
    var visible = rows.filter(function (row) { return !row.classList.contains('hidden'); });
    counter.textContent = visible.length + ' rows';
  }

  function applyFilter() {
    var needle = searchBox.value.toLowerCase();
    rows.forEach(function (row) {
      var text = row.textContent.toLowerCase();
      row.classList.toggle('hidden', needle.length > 0 && text.indexOf(needle) === -1);
    });
    updateCount();
  }

  searchBox.addEventListener('input', applyFilter);

  var headers = Array.prototype.slice.call(table.tHead.rows[0].cells);
  headers.forEach(function (header, columnIndex) {
    var ascending = true;
    header.addEventListener('click', function () {
      rows.sort(function (a, b) {
        var left = a.cells[columnIndex].textContent;
        var right = b.cells[columnIndex].textContent;
        var result = left.localeCompare(right, undefined, { numeric: true });
        return ascending ? result : -result;
      });
      ascending = !ascending;
      rows.forEach(function (row) { tbody.appendChild(row); });
    });
  });

  applyFilter();
})();
'''


def _cell_text(value: object) -> str:
    return '' if value is None else str(value)


def _row_cells(columns: Sequence[Column], row: Mapping[str, object]) -> str:
    return ''.join(
        f'<td>{html.escape(_cell_text(row.get(column.key)))}</td>' for column in columns
    )


def render_table_html(
    *,
    title: str,
    generated_at: str,
    columns: Sequence[Column],
    rows: Sequence[Mapping[str, object]],
) -> str:
    header_cells = ''.join(f'<th>{html.escape(column.label)}</th>' for column in columns)
    body_rows = ''.join(f'<tr>{_row_cells(columns, row)}</tr>' for row in rows)
    rows_json = _script_safe(json.dumps(list(rows)))

    return _PAGE_TEMPLATE.format(
        title=html.escape(title),
        generated_at=html.escape(generated_at),
        header_cells=header_cells,
        body_rows=body_rows,
        rows_json=rows_json,
        app_js=_APP_JS,
    )
