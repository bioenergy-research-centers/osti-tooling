import unittest

from downstream_sync import normalize_dataset_url, normalize_dataset_urls


class NormalizeDatasetUrlTests(unittest.TestCase):
    def test_extracts_href_from_link_object(self):
        self.assertEqual(
            normalize_dataset_url(
                {"rel": "citation", "href": "https://www.osti.gov/biblio/1346628"}
            ),
            "https://www.osti.gov/biblio/1346628",
        )

    def test_repairs_percent_encoded_link_object(self):
        self.assertEqual(
            normalize_dataset_url(
                "%7B'rel':%20'citation',%20'href':%20"
                "'https://www.osti.gov/biblio/1346628'%7D"
            ),
            "https://www.osti.gov/biblio/1346628",
        )

    def test_encodes_spaces_in_existing_url(self):
        self.assertEqual(
            normalize_dataset_url(
                "https://labkey.ornl.gov/labkey/Public Data/example/project-begin.view?"
            ),
            "https://labkey.ornl.gov/labkey/Public%20Data/example/project-begin.view?",
        )

    def test_normalizes_after_additive_merge(self):
        datasets = [
            {"dataset_url": "https://example.org/path with spaces"},
            {"dataset_url": "https://example.org/already-valid"},
        ]
        self.assertEqual(normalize_dataset_urls(datasets), 1)
        self.assertEqual(
            datasets[0]["dataset_url"], "https://example.org/path%20with%20spaces"
        )


if __name__ == "__main__":
    unittest.main()
