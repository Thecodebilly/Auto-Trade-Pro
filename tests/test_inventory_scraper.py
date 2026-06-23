from __future__ import annotations

import json

from autotrade_pro.inventory_scraper import (
    _discover_inventory_pages,
    _extract_json_feed,
    _extract_xml_feed,
    _normalise,
    _vehicles_from_playwright_response,
)


def test_normalise_prefers_real_vehicle_photos_over_placeholders_and_renders():
    vehicle = _normalise(
        {
            "year": 2026,
            "make": "Toyota",
            "model": "RAV4",
            "images": [
                "https://dealer.example.com/logo.png",
                "https://media.dealeralchemist.com/jellies/Toyota/RAV4/side.png?auto=format",
                "https://assets.cai-media-management.com/resize/640x640/common-vehicle-media/real-front.jpg",
                "https://dealer.example.com/no-photo.jpg",
                "https://media.dealeralchemist.com/jellies/Toyota/RAV4/front.png?auto=format",
            ],
        }
    )

    assert vehicle["images"] == [
        "https://assets.cai-media-management.com/resize/640x640/common-vehicle-media/real-front.jpg",
        "https://media.dealeralchemist.com/jellies/Toyota/RAV4/front.png?auto=format",
        "https://media.dealeralchemist.com/jellies/Toyota/RAV4/side.png?auto=format",
        "https://dealer.example.com/logo.png",
        "https://dealer.example.com/no-photo.jpg",
    ]


def test_normalise_keeps_generated_front_render_when_no_real_photo_exists():
    vehicle = _normalise(
        {
            "year": 2027,
            "make": "Toyota",
            "model": "Land Cruiser",
            "images": [
                "https://media.dealeralchemist.com/jellies/Toyota/Land-Cruiser/back.png?auto=format",
                "https://media.dealeralchemist.com/jellies/Toyota/Land-Cruiser/front.png?auto=format",
                "https://media.dealeralchemist.com/jellies/Toyota/Land-Cruiser/side.png?auto=format",
            ],
        }
    )

    assert vehicle["images"][0].endswith("/front.png?auto=format")


def test_json_feed_extraction_does_not_stop_at_legacy_500_vehicle_cap():
    payload = {
        "rows": [
            {
                "modelYear": 2025,
                "makeName": "Toyota",
                "modelName": f"Camry {index}",
                "stockNumber": f"STK{index:04d}",
                "internetPrice": "31500",
                "vdpUrl": f"/inventory/camry-{index}",
                "imageUrls": [
                    {"url": f"/images/camry-{index}.jpg"},
                    f"https://cdn.example.test/camry-{index}.webp",
                ],
            }
            for index in range(650)
        ]
    }

    vehicles = _extract_json_feed(
        json.dumps(payload),
        "https://dealer.example.com/new-vehicles/",
    )

    assert len(vehicles) == 650
    assert vehicles[-1]["stock_number"] == "STK0649"
    assert vehicles[0]["detail_url"] == "https://dealer.example.com/inventory/camry-0"
    assert vehicles[0]["images"] == [
        "https://dealer.example.com/images/camry-0.jpg",
        "https://cdn.example.test/camry-0.webp",
    ]


def test_xml_feed_extraction_does_not_stop_at_legacy_300_vehicle_cap():
    xml = "<vehicles>" + "".join(
        f"""
        <vehicle>
          <year>2024</year>
          <make>Honda</make>
          <model>Accord {index}</model>
          <stockNumber>HON{index:04d}</stockNumber>
          <price>29995</price>
        </vehicle>
        """
        for index in range(325)
    ) + "</vehicles>"

    vehicles = _extract_xml_feed(xml, "https://dealer.example.com/used-vehicles/")

    assert len(vehicles) == 325
    assert vehicles[-1]["stock_number"] == "HON0324"


def test_inventory_page_discovery_follows_categories_not_vehicle_detail_pages():
    html = """
      <a href="/new-vehicles/">New Vehicles</a>
      <a href="/used-vehicles/">Used Inventory</a>
      <a href="/inventory/">All Inventory</a>
      <a href="/inventory/new-2024-toyota-camry-se-4T1G11AK1RU000001/">2024 Toyota Camry</a>
      <a href="/service/">Schedule Service</a>
    """

    links = _discover_inventory_pages(html, "https://dealer.example.com/")

    assert links == [
        "https://dealer.example.com/new-vehicles/",
        "https://dealer.example.com/used-vehicles/",
        "https://dealer.example.com/inventory/",
    ]


def test_playwright_response_json_is_mapped_like_inventory_api_data():
    class FakeResponse:
        status = 200
        url = "https://dealer.example.com/api/search"
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "vehicleResults": [
                    {
                        "year": 2026,
                        "make": "Subaru",
                        "model": "Outback",
                        "stockNo": "SUB1001",
                        "displayPrice": "$36,450",
                        "photoUrls": [{"src": "//cdn.example.test/subaru.jpg"}],
                    }
                ]
            }

    vehicles = _vehicles_from_playwright_response(FakeResponse())

    assert vehicles == [
        {
            "year": 2026,
            "make": "Subaru",
            "model": "Outback",
            "stock_number": "SUB1001",
            "price": 36450,
            "images": ["https://cdn.example.test/subaru.jpg"],
        }
    ]
