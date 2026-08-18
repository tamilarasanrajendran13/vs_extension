import pytest

from datacompare.readers.xml import flatten_records
from datacompare.engines.polars_engine import PolarsEngine
from datacompare.readers.xml import read
from datacompare.config import Source


SIMPLE = b"""<orders>
  <order>
    <order_id>1</order_id>
    <customer>Alice</customer>
    <amount>10.0</amount>
  </order>
  <order>
    <order_id>2</order_id>
    <customer>Bob</customer>
    <amount>20.0</amount>
  </order>
</orders>"""


def test_flatten_basic():
    rows = flatten_records(SIMPLE, "./order")
    assert len(rows) == 2
    assert rows[0] == {"order_id": "1", "customer": "Alice", "amount": "10.0"}
    assert rows[1]["customer"] == "Bob"


def test_flatten_attributes_and_nested():
    xml = b"""<root>
      <rec id="A">
        <addr>
          <city>NYC</city>
          <zip>10001</zip>
        </addr>
      </rec>
    </root>"""
    rows = flatten_records(xml, "./rec")
    assert rows[0]["@id"] == "A"
    assert rows[0]["addr.city"] == "NYC"
    assert rows[0]["addr.zip"] == "10001"


def test_flatten_repeated_children_indexed():
    xml = b"""<root>
      <rec>
        <item>x</item>
        <item>y</item>
      </rec>
    </root>"""
    rows = flatten_records(xml, "./rec")
    assert rows[0]["item[0]"] == "x"
    assert rows[0]["item[1]"] == "y"


def test_namespaces_stripped():
    xml = b"""<ns:root xmlns:ns="http://example.com">
      <ns:rec><ns:val>7</ns:val></ns:rec>
    </ns:root>"""
    rows = flatten_records(xml, "./rec")
    assert rows[0]["val"] == "7"


def test_no_match_raises():
    with pytest.raises(ValueError):
        flatten_records(SIMPLE, "./nonexistent")


def test_read_into_engine_frame(tmp_path):
    p = tmp_path / "o.xml"
    p.write_bytes(SIMPLE)
    eng = PolarsEngine()
    src = Source(path=str(p), record_path="./order")
    frame = read(eng, src)
    assert eng.row_count(frame) == 2
    assert set(eng.columns(frame)) == {"order_id", "customer", "amount"}
