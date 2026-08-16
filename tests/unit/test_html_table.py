import json

from djlib.export.html_table import Column, render_table_html


def test_render_table_html_contains_title_and_generated_at() -> None:
    html = render_table_html(
        title='djlib catalogue',
        generated_at='2026-08-16T00:00:00+00:00',
        columns=[Column(key='a', label='A'), Column(key='b', label='B')],
        rows=[{'a': '1', 'b': '2'}],
    )
    assert 'djlib catalogue' in html
    assert '2026-08-16T00:00:00+00:00' in html


def test_render_table_html_has_one_table_and_one_json_script_block() -> None:
    html = render_table_html(
        title='t', generated_at='g', columns=[Column(key='a', label='A')], rows=[{'a': 'x'}]
    )
    assert html.count('<table') == 1
    assert html.count('<script type="application/json"') == 1
    assert '<th' in html


def test_render_table_html_embeds_row_values_as_json() -> None:
    html = render_table_html(
        title='t',
        generated_at='g',
        columns=[Column(key='a', label='A')],
        rows=[{'a': 'hello world'}, {'a': 'second'}],
    )
    start = html.index('<script type="application/json"')
    start = html.index('>', start) + 1
    end = html.index('</script>', start)
    payload = json.loads(html[start:end])
    assert payload == [{'a': 'hello world'}, {'a': 'second'}]


def test_render_table_html_renders_none_values_as_blank_cells() -> None:
    html = render_table_html(
        title='t',
        generated_at='g',
        columns=[Column(key='a', label='A'), Column(key='b', label='B')],
        rows=[{'a': None, 'b': 'present'}],
    )
    assert '<td></td><td>present</td>' in html
    assert 'None' not in html


def test_render_table_html_defangs_closing_script_tag_in_row_values() -> None:
    html = render_table_html(
        title='t',
        generated_at='g',
        columns=[Column(key='a', label='A')],
        rows=[{'a': '</script><script>alert(1)</script>'}],
    )
    assert '</script><script>alert(1)</script>' not in html
    start = html.index('<script type="application/json"')
    start = html.index('>', start) + 1
    end = html.index('</script>', start)
    payload = json.loads(html[start:end])
    assert payload == [{'a': '</script><script>alert(1)</script>'}]
