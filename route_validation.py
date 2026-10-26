"""現在位置と予定経路の整合性を確認する共通処理。"""

import math


def haversine_km(lat1, lon1, lat2, lon2):
    """2地点間の大圏距離(km)。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _latlon_vector(lat, lon):
    phi, lam = math.radians(lat), math.radians(lon)
    return (math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))


def _vector_latlon(vector):
    x, y, z = vector
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def initial_bearing(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)
    y = math.sin(delta_lon) * math.cos(phi2)
    x = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lon)
    )
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def heading_difference(first, second):
    return abs((first - second + 180) % 360 - 180)


def route_matches_position(
    route, lat, lon, max_distance_km=600, track=None, altitude=None,
    vertical_rate=None,
):
    """現在位置・進行方向・飛行段階が予定経路と矛盾しないか判定する。"""
    if not route or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    try:
        origin = route["origin"]
        destination = route["destination"]
        lat1, lon1 = float(origin["latitude"]), float(origin["longitude"])
        lat2, lon2 = float(destination["latitude"]), float(destination["longitude"])
    except (KeyError, TypeError, ValueError):
        return False

    start = _latlon_vector(lat1, lon1)
    end = _latlon_vector(lat2, lon2)
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(start, end))))
    omega = math.acos(dot)
    sin_omega = math.sin(omega)
    nearest = float("inf")
    for index in range(73):
        fraction = index / 72
        if abs(sin_omega) < 1e-9:
            vector = tuple((1 - fraction) * a + fraction * b for a, b in zip(start, end))
        else:
            left = math.sin((1 - fraction) * omega) / sin_omega
            right = math.sin(fraction * omega) / sin_omega
            vector = tuple(left * a + right * b for a, b in zip(start, end))
        sample_lat, sample_lon = _vector_latlon(vector)
        nearest = min(nearest, haversine_km(lat, lon, sample_lat, sample_lon))
    if nearest > max_distance_km:
        return False

    distance_from_origin = haversine_km(lat, lon, lat1, lon1)
    distance_to_destination = haversine_km(lat, lon, lat2, lon2)
    try:
        altitude_value = float(altitude)
        vertical_rate_value = float(vertical_rate)
    except (TypeError, ValueError):
        altitude_value = vertical_rate_value = None
    terminal_radius_km = 250
    if altitude_value is not None and altitude_value <= 5000:
        if vertical_rate_value is not None and vertical_rate_value >= 1:
            if distance_from_origin > terminal_radius_km:
                return False
        elif vertical_rate_value is not None and vertical_rate_value <= -1:
            if distance_to_destination > terminal_radius_km:
                return False

    try:
        track_value = float(track) % 360
    except (TypeError, ValueError):
        track_value = None
    if (
        track_value is not None
        and distance_from_origin > terminal_radius_km
        and distance_to_destination > terminal_radius_km
    ):
        destination_bearing = initial_bearing(lat, lon, lat2, lon2)
        if heading_difference(track_value, destination_bearing) > 120:
            return False
    return True
