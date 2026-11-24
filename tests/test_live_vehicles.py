import time
import unittest
from collections import deque

import app
from google.transit import gtfs_realtime_pb2


class LiveVehicleEndpointTests(unittest.TestCase):
    def setUp(self):
        self.original_positions = dict(app.live_vehicle_positions)
        self.original_previous_positions = dict(app.live_previous_positions)
        self.original_speed_history = {
            route_id: deque(samples)
            for route_id, samples in app.live_speed_history.items()
        }
        self.original_route_speeds = dict(app.live_route_speeds)
        self.original_status = dict(app.live_feed_status)
        app.live_vehicle_positions.clear()
        now = int(time.time())
        app.live_vehicle_positions.update({
            "inside": {
                "vehicle_id": "inside",
                "route_id": "2204",
                "latitude": 28.6,
                "longitude": 77.2,
                "timestamp": now,
            },
            "outside": {
                "vehicle_id": "outside",
                "route_id": "2205",
                "latitude": 29.5,
                "longitude": 77.2,
                "timestamp": now,
            },
            "stale": {
                "vehicle_id": "stale",
                "route_id": "2206",
                "latitude": 28.6,
                "longitude": 77.2,
                "timestamp": now - 181,
            },
        })

    def tearDown(self):
        app.live_vehicle_positions.clear()
        app.live_vehicle_positions.update(self.original_positions)
        app.live_previous_positions.clear()
        app.live_previous_positions.update(self.original_previous_positions)
        app.live_speed_history.clear()
        app.live_speed_history.update(self.original_speed_history)
        app.live_route_speeds.clear()
        app.live_route_speeds.update(self.original_route_speeds)
        app.live_feed_status.clear()
        app.live_feed_status.update(self.original_status)

    def test_feed_ingestion_stores_vehicle_coordinates(self):
        now = int(time.time())
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = now
        entity = feed.entity.add()
        entity.id = "synthetic-entity"
        vehicle = entity.vehicle
        vehicle.vehicle.id = "synthetic-vehicle"
        vehicle.trip.route_id = "2204"
        vehicle.position.latitude = 28.6
        vehicle.position.longitude = 77.2
        vehicle.timestamp = now

        app.update_live_speeds(feed)

        self.assertAlmostEqual(
            app.live_vehicle_positions["synthetic-vehicle"]["latitude"],
            28.6,
            places=5,
        )
        self.assertAlmostEqual(
            app.live_vehicle_positions["synthetic-vehicle"]["longitude"],
            77.2,
            places=5,
        )
        self.assertEqual(
            app.live_vehicle_positions["synthetic-vehicle"]["route_id"],
            "2204",
        )

    def test_returns_only_fresh_positions_inside_viewport(self):
        result = app.get_live_vehicles(
            west=77.0,
            south=28.0,
            east=78.0,
            north=29.0,
            limit=1500,
        )

        self.assertEqual([vehicle["vehicle_id"] for vehicle in result["vehicles"]], ["inside"])
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["truncated"])

    def test_applies_response_limit(self):
        now = int(time.time())
        app.live_vehicle_positions["newer"] = {
            "vehicle_id": "newer",
            "route_id": "2207",
            "latitude": 28.7,
            "longitude": 77.3,
            "timestamp": now + 1,
        }

        result = app.get_live_vehicles(
            west=77.0,
            south=28.0,
            east=78.0,
            north=29.0,
            limit=1,
        )

        self.assertEqual(result["count"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["vehicles"][0]["vehicle_id"], "newer")


if __name__ == "__main__":
    unittest.main()
