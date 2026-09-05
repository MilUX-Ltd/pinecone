"""Spec 001, criterion 2: discovery reports each fact and where it came from, and never a password.

The functions under test take their inputs as text so the suite runs on any machine; the
gathering on a real box is a thin layer over them, proven live at UAT.
"""

from __future__ import annotations

import json

from conftest import load

CORECONFIG = """<?xml version="1.0"?>
<Configuration xmlns="http://bbn.com/marti/xml/config">
  <network multicastTTL="5"><input _name="stdssl" protocol="tls" port="8089"/></network>
  <repository enable="true">
    <connection url="jdbc:postgresql://127.0.0.1:5432/cot" username="martiuser" password="SECRETPASS"/>
  </repository>
</Configuration>
"""
RETENTION = """---
dataRetentionMap:
  cot: null
  files: null
  missionpackages: null
  missions: null
  geochat: null
retentionSettings:
  files:
    exemptKeywords: []
"""
SS = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0      4096   0.0.0.0:8089      0.0.0.0:*
LISTEN 0      4096   0.0.0.0:8443      0.0.0.0:*
LISTEN 0      244    127.0.0.1:5432    0.0.0.0:*
LISTEN 0      128    [::]:22           [::]:*
"""


def test_version_comes_from_the_package_manager() -> None:
    d = load("pinecone_discover")
    assert d.parse_version("takserver 5.8-RELEASE75\n", "") == ("5.8-RELEASE75", "dpkg")
    assert d.parse_version("", "takserver-5.8-RELEASE75.noarch\n") == ("5.8-RELEASE75", "rpm")
    assert d.parse_version("", "") == (None, "not found")


def test_database_location_comes_from_coreconfig_without_the_password() -> None:
    d = load("pinecone_discover")
    c = d.parse_connection(CORECONFIG, "/opt/tak/CoreConfig.xml")
    assert c == {
        "host": "127.0.0.1",
        "port": 5432,
        "database": "cot",
        "username": "martiuser",
        "source": "/opt/tak/CoreConfig.xml",
    }
    assert d.parse_connection("<Configuration/>", "/opt/tak/CoreConfig.xml") is None


def test_listening_ports_are_read_from_ss() -> None:
    d = load("pinecone_discover")
    ports = d.listening_ports(SS)
    assert ports == {8089, 8443, 5432, 22}
    r = d.tak_ports_report(ports)
    assert r["8089"] is True and r["8446"] is False and r["5432"] is True and "22" not in r


def test_file_permissions_produce_the_example_file_finding() -> None:
    d = load("pinecone_discover")
    f = d.file_findings(
        {"/opt/tak/CoreConfig.xml": ("600", "tak", "tak"), "/opt/tak/CoreConfig.example.xml": ("674", "tak", "tak")}
    )
    assert f["/opt/tak/CoreConfig.xml"]["finding"] is None
    assert "world-readable" in f["/opt/tak/CoreConfig.example.xml"]["finding"]
    g = d.file_findings({"/opt/tak/CoreConfig.example.xml": ("600", "tak", "tak")})
    assert g["/opt/tak/CoreConfig.example.xml"]["finding"] is None


def test_retention_ttls_and_whether_anything_is_purged() -> None:
    d = load("pinecone_discover")
    r = d.parse_retention(RETENTION)
    assert r["ttls"] == {"cot": None, "files": None, "missionpackages": None, "missions": None, "geochat": None}
    assert r["purges"] is False
    assert d.parse_retention(RETENTION.replace("cot: null", "cot: 30"))["purges"] is True


def test_the_report_composes_every_fact_with_its_source() -> None:
    d = load("pinecone_discover")
    rep = d.report(
        version=("5.8-RELEASE75", "dpkg"),
        unit_state="active",
        ports=d.tak_ports_report({8089, 8443, 5432}),
        connection=d.parse_connection(CORECONFIG, "/opt/tak/CoreConfig.xml"),
        files=d.file_findings({"/opt/tak/CoreConfig.example.xml": ("674", "tak", "tak")}),
        retention=d.parse_retention(RETENTION),
        retention_source="/opt/tak/conf/retention/retention-policy.yml",
        timezone="America/New_York",
        rows=("148298", "398", "2026-08-08 14:37:52+00", "2026-09-04 11:39:43+00"),
        credential={"role": "pinecone", "grant": "SELECT on cot_router", "created": True},
    )
    assert rep["tak"]["version"] == "5.8-RELEASE75" and rep["tak"]["version_source"] == "dpkg"
    assert rep["database"]["source"] == "/opt/tak/CoreConfig.xml"
    assert rep["retention"]["purges"] is False and rep["retention"]["source"].endswith("retention-policy.yml")
    assert rep["database"]["timezone"] == "America/New_York"
    assert rep["rows"]["count"] == 148298 and rep["rows"]["oldest"].startswith("2026-08-08")
    assert rep["credential"]["grant"] == "SELECT on cot_router"
    text = d.render_text(rep)
    for needle in (
        "5.8-RELEASE75",
        "dpkg",
        "CoreConfig.xml",
        "world-readable",
        "nothing is purged",
        "America/New_York",
        "148,298",
        "SELECT on cot_router",
    ):
        assert needle in text, needle


def test_report_never_carries_a_password() -> None:
    d = load("pinecone_discover")
    rep = d.report(
        version=("5.8-RELEASE75", "dpkg"),
        unit_state="active",
        ports=d.tak_ports_report(set()),
        connection=d.parse_connection(CORECONFIG, "/opt/tak/CoreConfig.xml"),
        files={},
        retention=d.parse_retention(RETENTION),
        retention_source="x",
        timezone="UTC",
        rows=None,
        credential={"role": "pinecone", "grant": "SELECT on cot_router", "created": False},
    )
    blob = json.dumps(rep) + d.render_text(rep)
    assert "SECRETPASS" not in blob
    assert "password" not in json.dumps(rep).lower()
