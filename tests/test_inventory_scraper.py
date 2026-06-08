from __future__ import annotations

from autotrade_pro.inventory_scraper import _normalise


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
