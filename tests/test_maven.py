"""Изолированные тесты Maven POM: все сетевые соединения запрещены."""

import io
import socket
import unittest
import urllib.error
from unittest.mock import patch
from xml.sax.saxutils import escape

from src.errors import ToolError
from src.maven import MavenRepository, _NoRedirect


BASE = "https://repo.example.test/maven2"


def pom(coord, body="", parent="", namespace=True, inherited=False):
    """Построить небольшой опубликованный POM для фиктивного репозитория."""
    group, artifact, version = coord.split(":")
    ns = ' xmlns="http://maven.apache.org/POM/4.0.0"' if namespace else ""
    group_tag = "" if inherited else f"<groupId>{group}</groupId>"
    version_tag = "" if inherited else f"<version>{version}</version>"
    return (f"<project{ns}><modelVersion>4.0.0</modelVersion>{parent}"
            f"{group_tag}<artifactId>{artifact}</artifactId>{version_tag}"
            f"{body}</project>")


def parent_xml(coord):
    """Построить объявление родительского артефакта."""
    group, artifact, version = coord.split(":")
    return (f"<parent><groupId>{group}</groupId>"
            f"<artifactId>{artifact}</artifactId>"
            f"<version>{version}</version></parent>")


def dep(coord, **fields):
    """Построить зависимость с произвольными дополнительными полями."""
    group, artifact = coord.split(":")
    values = {"groupId": group, "artifactId": artifact, **fields}
    contents = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in values.items())
    return f"<dependency>{contents}</dependency>"


def deps(*items):
    """Обернуть объявления в секцию dependencies."""
    return "<dependencies>" + "".join(items) + "</dependencies>"


def management(*items):
    """Построить секцию dependencyManagement."""
    return "<dependencyManagement>" + deps(*items) + "</dependencyManagement>"


class MavenTestCase(unittest.TestCase):
    """Предоставить фиктивные HTTP-ответы и запретить реальную сеть."""

    def setUp(self):
        """Подменить HTTP opener и запретить любые исходящие соединения."""
        self.responses = {}
        self.calls = []
        opener = patch("src.maven.urllib.request.build_opener",
                       return_value=self)
        error = AssertionError("Реальная сеть в тестах запрещена")
        network = patch.object(socket.socket, "connect", side_effect=error)
        opener.start()
        network.start()
        self.addCleanup(opener.stop)
        self.addCleanup(network.stop)
        self.repo = MavenRepository(BASE)

    def install(self, coord, contents):
        """Добавить XML или исключение в фиктивное хранилище."""
        group, artifact, version = coord.split(":")
        path = group.replace(".", "/")
        url = f"{BASE}/{path}/{artifact}/{version}/{artifact}-{version}.pom"
        self.responses[url] = contents

    def open(self, url, timeout):
        """Имитировать opener.open без фактических обращений к сети."""
        self.calls.append((url, timeout))
        if url not in self.responses:
            raise AssertionError(f"Неожиданный запрос: {url}")
        data = self.responses[url]
        if isinstance(data, Exception):
            raise data
        if isinstance(data, str):
            data = data.encode("utf-8")
        return io.BytesIO(data)


class MavenDependenciesTests(MavenTestCase):
    """Проверки получения и отбора прямых зависимостей."""

    def test_scopes_optional_namespaces_and_skipped_versions(self):
        """Учитывать compile/runtime и игнорировать исключённые зависимости."""
        body = deps(
            dep("g:compile", version="1"),
            dep("g:runtime", version="2", scope="runtime"),
            dep("g:test", version="${unknown}", scope="test",
                optional="${unknown}"),
            dep("g:provided", scope="provided"),
            dep("g:system", scope="system"),
            dep("g:import", scope="import"),
            dep("g:opt", version="${unknown}", optional="TRUE"),
            dep("g:normal", version="3", optional="false"),
        )
        self.install("sample:app:1", pom("sample:app:1", body))
        actual = self.repo.dependencies("sample:app:1")
        self.assertEqual(actual, ["g:compile:1", "g:runtime:2", "g:normal:3"])
        self.assertEqual(self.calls[0][1], 20)

    def test_without_namespace_and_with_nested_properties(self):
        """Разрешать вложенные свойства и псевдонимы Maven без namespace."""
        props = ("<properties><revision>${project.version}</revision>"
                 "<nested>${revision}</nested></properties>")
        body = props + deps(dep("org.example:lib", version="${nested}"))
        self.install("org.app:app:2.1", pom("org.app:app:2.1", body,
                                            namespace=False))
        self.assertEqual(self.repo.dependencies("org.app:app:2.1"),
                         ["org.example:lib:2.1"])

    def test_excluded_dependency_does_not_need_other_properties(self):
        """Не разрешать недоступные поля исключённых зависимостей."""
        body = deps(dep("g:a", scope="${missing}", optional="true"),
                    dep("g:b", scope="test", optional="${missing}"))
        self.install("g:app:1", pom("g:app:1", body))
        self.assertEqual(self.repo.dependencies("g:app:1"), [])

    def test_parent_management_and_inherited_dependencies(self):
        """Наследовать управление версиями, свойства и прямые зависимости."""
        parent = "org.demo:parent:1"
        child = "org.demo:child:1"
        p_body = ("<properties><lib.version>2</lib.version></properties>" +
                  management(dep("g:managed", version="${lib.version}")) +
                  deps(dep("g:inherited", version="1"),
                       dep("g:override", version="1", scope="test")))
        c_body = ("<properties><lib.version>3</lib.version></properties>" +
                  deps(dep("g:managed"), dep("g:override", scope="runtime")))
        self.install(parent, pom(parent, p_body))
        self.install(child, pom(child, c_body, parent_xml(parent),
                                inherited=True))
        expected = ["g:inherited:1", "g:override:1", "g:managed:3"]
        self.assertEqual(self.repo.dependencies(child), expected)
        self.assertEqual(len(self.calls), 2)

    def test_child_management_override_and_parent_aliases(self):
        """Переопределять управление и разрешать свойства родителя."""
        parent = "org.demo:parent:7"
        child = "org.demo:child:7"
        p_body = management(dep("g:lib", version="1"))
        c_body = (management(dep("g:lib",
                                 version="${project.parent.version}")) +
                  deps(dep("g:lib")))
        self.install(parent, pom(parent, p_body))
        self.install(child, pom(child, c_body, parent_xml(parent),
                                inherited=True))
        self.assertEqual(self.repo.dependencies(child), ["g:lib:7"])

    def test_management_scope_optional_and_direct_override(self):
        """Применять managed scope и optional, если они не заданы явно."""
        managed = management(
            dep("g:skipped", version="1", scope="test"),
            dep("g:also-skipped", version="1", optional="true"),
            dep("g:included", version="1", scope="test"),
            dep("g:explicit", version="1"),
        )
        body = managed + deps(dep("g:skipped"), dep("g:also-skipped"),
                              dep("g:included", scope="runtime"),
                              dep("g:explicit", version="2"))
        self.install("sample:app:1", pom("sample:app:1", body))
        self.assertEqual(self.repo.dependencies("sample:app:1"),
                         ["g:included:1", "g:explicit:2"])

    def test_management_matches_type_and_classifier(self):
        """Не смешивать версии артефактов с разным типом или classifier."""
        managed = management(dep("g:lib", version="1"),
                             dep("g:lib", version="2", type="test-jar",
                                 classifier="tests"))
        second = dep("g:lib", type="test-jar", classifier="tests")
        body = managed + deps(dep("g:lib"), second)
        self.install("sample:app:1", pom("sample:app:1", body))
        self.assertEqual(self.repo.dependencies("sample:app:1"),
                         ["g:lib:1", "g:lib:2"])

    def test_parent_coordinates_may_use_child_property(self):
        """Разрешать локальные свойства в координатах родителя."""
        parent = "org.demo:parent:4"
        child = "org.demo:child:4"
        parent_ref = parent_xml("org.demo:parent:${revision}")
        body = "<properties><revision>4</revision></properties>"
        self.install(parent, pom(parent))
        self.install(child, pom(child, body, parent_ref, inherited=True))
        self.assertEqual(self.repo.dependencies(child), [])

    def test_import_bom_skipped_when_version_is_explicit(self):
        """Пропускать BOM без загрузки и не мешать явным версиям."""
        body = (management(dep("g:bom", version="1", type="pom",
                               scope="import")) +
                deps(dep("g:lib", version="2")))
        self.install("sample:app:1", pom("sample:app:1", body))
        self.assertEqual(self.repo.dependencies("sample:app:1"), ["g:lib:2"])
        self.assertEqual(len(self.calls), 1)

    def test_cache_returns_copy_and_reuses_parent(self):
        """Кэшировать POM и результаты, не отдавая изменяемый кэш наружу."""
        parent = "g:parent:1"
        first = "g:first:1"
        second = "g:second:1"
        self.install(parent, pom(parent, deps(dep("g:lib", version="2"))))
        self.install(first, pom(first, parent=parent_xml(parent)))
        self.install(second, pom(second, parent=parent_xml(parent)))
        result = self.repo.dependencies(first)
        result.append("g:fake:1")
        self.assertEqual(self.repo.dependencies(first), ["g:lib:2"])
        self.assertEqual(self.repo.dependencies(second), ["g:lib:2"])
        self.assertEqual(len(self.calls), 3)


class MavenErrorTests(MavenTestCase):
    """Проверки ошибок данных, сети и безопасности."""

    def test_invalid_coordinates_never_reach_network(self):
        """Отклонять невалидные координаты и обходы каталогов."""
        invalid = [None, "", "g:a", "g:a:v:x", "../g:a:1", "g:a:../1",
                   "g/a:a:1", "g:a:1/2", "g:a:1?x", "g:a:[1,2)",
                   "g..x:a:1", "g:a:1..2", "g::1", "g:a:1\n",
                   "g:a:LATEST", "g:a:RELEASE"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ToolError):
                self.repo.dependencies(value)
        self.assertEqual(self.calls, [])

    def test_invalid_base_urls(self):
        """Запрещать небезопасные схемы, учётные данные и сегменты пути."""
        invalid = [None, "", "file:///etc", "ftp://example.com", "https://",
                   "https://user:pass@example.com/repo", "https://host/../x",
                   "https://host/%2e%2e/x", "https://host/?query=1",
                   "https://host/#fragment", "https://host\\evil",
                   "https://host:bad", " https://host", "https://host:0"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ToolError):
                MavenRepository(value)
        self.assertEqual(self.calls, [])

    def test_http_error_contains_code_url_and_cause(self):
        """Сообщать код и адрес отсутствующего POM."""
        error = urllib.error.HTTPError(BASE, 404, "Not Found", {}, None)
        self.install("g:a:1", error)
        with self.assertRaises(ToolError) as caught:
            self.repo.dependencies("g:a:1")
        self.assertIn("404", str(caught.exception))
        self.assertIn("g/a/1/a-1.pom", str(caught.exception))
        self.assertIs(caught.exception.__cause__, error)

    def test_network_timeout_and_unavailable_pom(self):
        """Преобразовывать таймауты и проблемы соединения в ToolError."""
        errors = [TimeoutError("timeout"), urllib.error.URLError("offline"),
                  OSError("offline")]
        for error in errors:
            with self.subTest(error=error):
                self.install("g:a:1", error)
                with self.assertRaisesRegex(ToolError, "Не удалось загрузить"):
                    self.repo.dependencies("g:a:1")
        self.assertEqual(len(self.calls), len(errors))

    def test_redirects_are_not_followed(self):
        """Не разрешать переходы по HTTP redirect на произвольные адреса."""
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {},
                                                   "http://other.test"))
        error = urllib.error.HTTPError(BASE, 302, "Found", {}, None)
        self.install("g:a:1", error)
        with self.assertRaisesRegex(ToolError, "302.*перенаправления"):
            self.repo.dependencies("g:a:1")
        self.assertEqual(len(self.calls), 1)

    def test_bad_xml_and_wrong_root(self):
        """Отвергать повреждённый XML и некорректный корень POM."""
        for data, message in [("<project>", "XML"), ("<root/>", "project")]:
            with self.subTest(data=data):
                self.install("g:a:1", data)
                with self.assertRaisesRegex(ToolError, message):
                    self.repo.dependencies("g:a:1")

    def test_oversized_pom(self):
        """Не читать POM сверх заданного предела."""
        self.install("g:a:1", b"x" * 101)
        with patch("src.maven._MAX_POM_BYTES", 100):
            with self.assertRaisesRegex(ToolError, "размер"):
                self.repo.dependencies("g:a:1")

    def test_dtd_entity_rejected_in_utf8_and_utf16(self):
        """Запрещать DTD и сущности даже в XML с двухбайтовой кодировкой."""
        xml = '<!DOCTYPE project [<!ENTITY a "x">]><project>&a;</project>'
        for data in (xml.encode(), xml.encode("utf-16")):
            with self.subTest(encoding=data[:8]):
                self.install("g:a:1", data)
                with self.assertRaisesRegex(ToolError, "DTD/ENTITY"):
                    self.repo.dependencies("g:a:1")

    def test_unresolved_and_cyclic_properties(self):
        """Выявлять отсутствующие и циклические свойства зависимостей."""
        bodies = [deps(dep("g:lib", version="${missing}")),
                  "<properties><x>${y}</x><y>${x}</y></properties>" +
                  deps(dep("g:lib", version="${x}"))]
        for body, message in zip(bodies, ("missing", "Цикл")):
            with self.subTest(message=message):
                repo = MavenRepository(BASE)
                self.install("g:a:1", pom("g:a:1", body))
                with self.assertRaisesRegex(ToolError, message):
                    repo.dependencies("g:a:1")

    def test_property_expansion_is_bounded(self):
        """Ограничивать размер подставляемых значений свойств."""
        body = ("<properties><x>aaaaaaaaaaaa</x></properties>" +
                deps(dep("g:lib", version="${x}${x}")))
        self.install("g:a:1", pom("g:a:1", body))
        with patch("src.maven._MAX_PROPERTY_SIZE", 16):
            with self.assertRaisesRegex(ToolError, "большое свойство"):
                self.repo.dependencies("g:a:1")

    def test_missing_and_bad_version(self):
        """Не придумывать отсутствующие версии и не принимать диапазоны."""
        for version in ("", "[1,2)", "LATEST", "RELEASE"):
            with self.subTest(version=version):
                repo = MavenRepository(BASE)
                self.install("g:a:1", pom("g:a:1", deps(
                    dep("g:lib", version=version))))
                with self.assertRaises(ToolError):
                    repo.dependencies("g:a:1")

    def test_import_bom_cannot_supply_version(self):
        """Явно сообщать об ограничении BOM при неизвестной версии."""
        body = (management(dep("g:bom", version="1", type="pom",
                               scope="import")) + deps(dep("g:lib")))
        self.install("g:a:1", pom("g:a:1", body))
        with self.assertRaisesRegex(ToolError, "версия.*BOM"):
            self.repo.dependencies("g:a:1")
        self.assertEqual(len(self.calls), 1)

    def test_parent_cycle_and_unavailable_parent(self):
        """Обнаруживать циклы родителей и сообщать ошибки загрузки родителя."""
        a = "g:a:1"
        b = "g:b:1"
        self.install(a, pom(a, parent=parent_xml(b)))
        self.install(b, pom(b, parent=parent_xml(a)))
        with self.assertRaisesRegex(ToolError, "Цикл родителей"):
            self.repo.dependencies(a)
        repo = MavenRepository(BASE)
        self.install(b, urllib.error.HTTPError(BASE, 404, "Missing", {}, None))
        with self.assertRaisesRegex(ToolError, "b-1.pom"):
            repo.dependencies(a)

    def test_mismatched_pom_identity(self):
        """Не принимать POM, описывающий иной артефакт."""
        self.install("g:a:1", pom("g:other:1"))
        with self.assertRaisesRegex(ToolError, "не совпадают"):
            self.repo.dependencies("g:a:1")

    def test_bad_scope_optional_and_parent_property(self):
        """Сообщать некорректные scope, optional и координату родителя."""
        bodies = [deps(dep("g:lib", version="1", scope="custom")),
                  deps(dep("g:lib", version="1", optional="maybe"))]
        for body in bodies:
            with self.subTest(body=body):
                repo = MavenRepository(BASE)
                self.install("g:a:1", pom("g:a:1", body))
                with self.assertRaises(ToolError):
                    repo.dependencies("g:a:1")
        repo = MavenRepository(BASE)
        root = pom("g:a:1", parent=parent_xml("g:p:${missing}"))
        self.install("g:a:1", root)
        with self.assertRaisesRegex(ToolError, "missing"):
            repo.dependencies("g:a:1")


if __name__ == "__main__":
    unittest.main()
