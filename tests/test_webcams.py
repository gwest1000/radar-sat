import unittest

from radarsat.webcams import normalize_cameras, project_cameras, public_url, drivebc_id


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value)
        for name, value in globals().items() if name.startswith("test_") and callable(value))


def test_drivebc_authoritative_record_replaces_wrong_arfi_thumbnail():
    arfi = [{"attributes": {"FID": 1, "Name": "Anarchist Pass", "Long": -119.47, "Lat": 49.07,
        "URL1": "https://images.drivebc.ca/bchighwaycam/pub/html/www/64.html",
        "Thumbnail1": "https://www.drivebc.ca/images/139.jpg"}}]
    official = [{"id": 64, "name": "Anarchist - W", "location": {"coordinates": [-119.46, 49.08]}}]
    cameras = normalize_cameras(arfi, official)
    assert len(cameras) == 1
    assert cameras[0]["longitude"] == -119.46
    assert cameras[0]["previews"][0]["url"].endswith("/64.jpg")
    assert cameras[0]["links"][0]["url"].endswith("/cameras/64")


def test_mountain_camera_keeps_all_views_and_webpage_links():
    record = {"FID": 2, "Name": "Mountain", "Long": -123, "Lat": 50,
              "URL1": "https://example.org/cameras", "URL2": "https://example.org/weather",
              "Thumbnail1": "https://example.org/latest.jpg", "Thumbnail2": "https://example.org/snow.jpg"}
    camera = normalize_cameras([{"attributes": record}], [])[0]
    assert len(camera["links"]) == 2
    assert len(camera["previews"]) == 2


def test_bc_camera_projects_into_all_domains_and_pacific_longitude_branch():
    cameras = [{"id": "vancouver", "longitude": -123.12, "latitude": 49.28},
               {"id": "toronto", "longitude": -79.38, "latitude": 43.65}]
    domains = project_cameras(cameras)
    assert {p["id"] for p in domains["bc"]} == {"vancouver"}
    assert {p["id"] for p in domains["north-america"]} == {"vancouver", "toronto"}
    assert any(p["id"] == "vancouver" and 0 < p["x"] < 1 for p in domains["north-pacific"])


def test_links_are_safe_and_disabled_cameras_are_omitted():
    assert public_url("javascript:alert(1)") is None
    assert public_url("https://user:password@example.org/") is None
    assert public_url("http://example.org/cam.jpg") == "https://example.org/cam.jpg"
    assert drivebc_id("https://notdrivebc.ca/1.html") is None
    assert normalize_cameras([], [{"id": 1, "is_on": False}]) == []
