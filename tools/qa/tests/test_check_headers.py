import unittest

from tools.qa import check_headers


class HeaderCheckTests(unittest.TestCase):
    def test_quoted_cppflags_preserve_spaces_and_embedded_equals(self):
        command = (
            r'cd "/tmp/source path" && BINDIR=/tmp/build '
            r'CPPFLAGS="-DM_CORE_GBA \"-I/tmp/include path\" -DVALUE=one" '
            r'/tmp/venv/bin/python /tmp/source/_builder.py'
        )
        self.assertEqual(
            check_headers.python_flags(command),
            ["-DM_CORE_GBA", "-I/tmp/include path", "-DVALUE=one"],
        )

    def test_missing_and_duplicate_builder_commands_fail(self):
        command = "cd /src && CPPFLAGS=-DM_CORE_GBA python _builder.py"
        for text in ("echo _builder.py", command + "\n" + command):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    check_headers.python_flags(text)

    def test_missing_and_duplicate_cppflags_assignments_fail(self):
        for flags in ("", "CPPFLAGS=-DONE CPPFLAGS=-DTWO"):
            with self.subTest(flags=flags):
                with self.assertRaisesRegex(ValueError, "CPPFLAGS"):
                    check_headers.python_flags(f"cd /src && {flags} python _builder.py")

    def test_malformed_shell_quoting_fails(self):
        with self.assertRaises(ValueError):
            check_headers.python_flags('cd /src && CPPFLAGS="unterminated python _builder.py')

    def test_native_assignments_preserve_flags(self):
        text = 'C_DEFINES = -DM_CORE_GBA -DVALUE=one\nC_INCLUDES = "-I/include path"\n'
        self.assertEqual(check_headers.assignment(text, "C_DEFINES"),
                         "-DM_CORE_GBA -DVALUE=one")
        self.assertEqual(check_headers.assignment(text, "C_INCLUDES"), '"-I/include path"')

    def test_empty_assignment_does_not_consume_the_next_line(self):
        self.assertEqual(
            check_headers.assignment("C_DEFINES = \nC_INCLUDES = -I/include\n", "C_DEFINES"),
            "",
        )

    def test_missing_and_duplicate_native_assignments_fail(self):
        for text in ("OTHER = -DONE", "C_DEFINES = -DONE\nC_DEFINES = -DTWO"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    check_headers.assignment(text, "C_DEFINES")

    def test_only_the_known_sysconf_function_is_normalized(self):
        text = """
            extern long int __sysconf (int name) __attribute__((__nothrow__));
            struct mCore { int field; };
            extern long int __sysconf_extra (int name);
            extern long int __sysconf;
        """
        self.assertEqual(check_headers.normalized(text), [
            "struct mCore { int field; };",
            "extern long int __sysconf_extra (int name);",
            "extern long int __sysconf;",
        ])

    def test_meaningful_declaration_differences_survive_normalization(self):
        first = "extern long int __sysconf (int name);\nvoid run(int value);\n"
        second = "extern long int __sysconf (int name) __attribute__((__leaf__));\nvoid run(long value);\n"
        self.assertNotEqual(check_headers.normalized(first), check_headers.normalized(second))
        self.assertNotEqual(check_headers.normalized("struct mCore { int field; };"),
                            check_headers.normalized("struct mCore { long field; };"))


if __name__ == "__main__":
    unittest.main()
