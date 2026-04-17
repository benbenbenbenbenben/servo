import unittest

from servo.size_report import (
    crate_name_from_path,
    parse_dynamic_library_paths,
    parse_linker_map_crates,
    parse_nm_symbols,
    parse_section_sizes,
)


class SizeReportHelpersTest(unittest.TestCase):
    def test_parse_section_sizes(self) -> None:
        output = """
section              size    addr
.text             1048576   4096
.rodata            262144 1052672
.data               65536 1314816
Total             1376256
"""
        self.assertEqual(
            parse_section_sizes(output),
            {
                ".text": 1048576,
                ".rodata": 262144,
                ".data": 65536,
            },
        )

    def test_parse_nm_symbols(self) -> None:
        output = """
0000000000000000 4096 T _ZN5servo4main17h1234567890abcdefE
0000000000001000 2048 T _ZN5servo8smaller17h1234567890abcdefE
"""
        self.assertEqual(
            parse_nm_symbols(output, 1),
            [("_ZN5servo4main17h1234567890abcdefE", 4096)],
        )

    def test_parse_dynamic_library_paths(self) -> None:
        output = """
        libfontconfig.so.1 => /usr/lib/x86_64-linux-gnu/libfontconfig.so.1 (0x00007f)
        libc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x00007f)
"""
        self.assertEqual(
            parse_dynamic_library_paths(output),
            [
                "/usr/lib/x86_64-linux-gnu/libfontconfig.so.1",
                "/usr/lib/x86_64-linux-gnu/libc.so.6",
            ],
        )

    def test_crate_name_from_path(self) -> None:
        self.assertEqual(
            crate_name_from_path("/tmp/target/release/deps/libscript-abc1234567890def.rlib(dom.o)"),
            "script",
        )
        self.assertEqual(
            crate_name_from_path("/tmp/target/release/deps/webrender-abcdef0123456789.12.rcgu.o"),
            "webrender",
        )

    def test_parse_linker_map_crates(self) -> None:
        output = """
                0x0000000000100000       0x2000 /tmp/target/release/deps/libscript-abc1234567890def.rlib(dom.o)
                0x0000000000120000       0x0800 /tmp/target/release/deps/libscript-abc1234567890def.rlib(layout.o)
                0x0000000000130000       0x0400 /tmp/target/release/deps/libwebrender-fedcba9876543210.rlib(render.o)
"""
        self.assertEqual(
            parse_linker_map_crates(output, 2),
            [("script", 0x2800), ("webrender", 0x0400)],
        )


if __name__ == "__main__":
    unittest.main()
