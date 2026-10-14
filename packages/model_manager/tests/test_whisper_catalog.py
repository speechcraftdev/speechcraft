"""Catalog contract for platform-specific full Whisper Large-v3 assets."""
import unittest

from speechcraft_models.manifest import ManifestError, load_catalog, select_artifact


class WhisperCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog()

    def test_linux_and_wsl_select_the_same_ctranslate2_variant(self):
        linux = select_artifact(self.catalog, 'asr', 'linux-x86_64')
        wsl = select_artifact(self.catalog, 'asr', 'windows-wsl2')
        self.assertEqual(linux, wsl)
        self.assertEqual(linux['source']['repo_id'], 'Systran/faster-whisper-large-v3')
        self.assertIn('CTranslate2', linux['format'])
        self.assertEqual(linux['revision'], 'edaa852ec7e145841d8ffdb056a99866b5f0a478')
        self.assertEqual({item['path'] for item in linux['required_files']}, {
            'config.json', 'model.bin', 'preprocessor_config.json',
            'tokenizer.json', 'vocabulary.json',
        })

    def test_macos_selects_distinct_full_mlx_large_v3_variant(self):
        mac = select_artifact(self.catalog, 'asr', 'macos-arm64')
        self.assertEqual(mac['source']['repo_id'], 'mlx-community/whisper-large-v3-mlx')
        self.assertIn('MLX', mac['format'])
        self.assertNotIn('4bit', mac['format'])
        self.assertEqual(mac['revision'], '49e6aa286ad60c14352c404340ded53710378a11')
        self.assertEqual({item['path'] for item in mac['required_files']},
                         {'config.json', 'weights.npz'})

    def test_both_inventories_have_exact_sizes_and_hashes(self):
        for platform in ('linux-x86_64', 'windows-wsl2', 'macos-arm64'):
            artifact = select_artifact(self.catalog, 'asr', platform)
            self.assertTrue(artifact['inventory_complete'])
            self.assertEqual(artifact['availability'], 'verified')
            self.assertEqual(artifact['release_blockers'], [])
            self.assertEqual(artifact['source']['type'], 'huggingface')
            self.assertRegex(artifact['revision'], r'^[0-9a-f]{40}$')
            for item in artifact['required_files']:
                self.assertGreater(item['size_bytes'], 0)
                self.assertRegex(item['sha256'], r'^[0-9a-f]{64}$')

    def test_unsupported_platform_is_rejected_without_fallback(self):
        with self.assertRaisesRegex(ManifestError, 'unsupported platform'):
            select_artifact(self.catalog, 'asr', 'windows-native')


if __name__ == '__main__':
    unittest.main()
