-- Large procedural seed for the Query Bench demo database.
--
-- Generates a sizeable, realistic, GEOSPATIAL retail dataset for demos:
--   * 100 stores      (PostGIS points across the Seattle metro)
--   * 5,000 customers (each with a PostGIS home location + signup date)
--   * 30,000 orders   (realistic status / amount / discount / refund / tax mix)
--   * 24 products     (descriptions; embeddings populated by seed_embeddings.py)
--
-- Coordinates use SRID 4326 (WGS84 lon/lat). The demo "downtown" reference
-- point is (-122.3321, 47.6062) — a cluster of stores
-- and customers sit within ~5 km of it so proximity queries return data.
--
-- Reproducible via setseed(). Guarded so re-running does not duplicate rows.

DO $seed$
DECLARE
    first_names TEXT[] := ARRAY['Ava','Liam','Mia','Noah','Emma','Oliver','Sophia',
        'Lucas','Isabella','Ethan','Amelia','Mason','Harper','Logan','Ella','James',
        'Aria','Ben','Chloe','Henry','Layla','Jack','Nora','Leo','Zoe'];
    last_names TEXT[] := ARRAY['Thompson','Nguyen','Patel','Garcia','Wilson','Brown',
        'Davis','Martin','Lee','Clark','Lewis','Walker','Hall','Young','King','Wright',
        'Scott','Green','Adams','Baker','Rivera','Hughes','Price','Bennett','Reed'];
    cities TEXT[] := ARRAY['Seattle','Bellevue','Redmond','Kirkland','Renton',
        'Tacoma','Everett','Bothell'];
BEGIN
    IF (SELECT count(*) FROM customers) > 0 THEN
        RAISE NOTICE 'Demo data already present — skipping seed.';
        RETURN;
    END IF;

    PERFORM setseed(0.42);

    -- ── Stores ───────────────────────────────────────────────────────────
    -- 15 stores clustered within ~3-4 km of downtown (the "near downtown" set).
    INSERT INTO stores (name, city, region, geom)
    SELECT
        'Store #' || g,
        'Seattle',
        'WA',
        ST_SetSRID(ST_MakePoint(
            -122.3321 + (random() - 0.5) * 0.06,
            47.6062  + (random() - 0.5) * 0.06
        ), 4326)
    FROM generate_series(1, 15) AS g;

    -- 85 stores scattered across the wider metro bounding box.
    INSERT INTO stores (name, city, region, geom)
    SELECT
        'Store #' || (g + 15),
        cities[1 + floor(random() * array_length(cities, 1))::int],
        'WA',
        ST_SetSRID(ST_MakePoint(
            -122.45 + random() * 0.35,
            47.45  + random() * 0.30
        ), 4326)
    FROM generate_series(1, 85) AS g;

    -- ── Customers ────────────────────────────────────────────────────────
    -- 5,000 customers across the metro, signup spread over ~3 years, each with
    -- a PostGIS home location for customer-proximity queries.
    INSERT INTO customers (name, email, signup_date, city, geom)
    SELECT
        first_names[1 + floor(random() * array_length(first_names, 1))::int]
            || ' ' ||
        last_names[1 + floor(random() * array_length(last_names, 1))::int],
        'customer' || g || '@example.com',
        (CURRENT_DATE - (floor(random() * 1095))::int)::date,
        cities[1 + floor(random() * array_length(cities, 1))::int],
        ST_SetSRID(ST_MakePoint(
            -122.45 + random() * 0.35,
            47.45  + random() * 0.30
        ), 4326)
    FROM generate_series(1, 5000) AS g;

    -- ── Orders ───────────────────────────────────────────────────────────
    -- 30,000 orders. status weighted 70% completed / 10% pending /
    -- 10% cancelled / 10% returned. Dates within the last 400 days so the
    -- governed "active customer" (completed in last 90 days) and "churned"
    -- (no completed order in 180 days) metrics are meaningful.
    INSERT INTO orders (customer_id, order_date, status, amount, discount, refund, tax)
    SELECT
        1 + floor(random() * 5000)::int,
        (CURRENT_DATE - (floor(random() * 400))::int)::date,
        (ARRAY['completed','completed','completed','completed','completed',
               'completed','completed','pending','cancelled','returned']
        )[1 + floor(random() * 10)::int],
        amt,
        round((amt * random() * 0.15)::numeric, 2),
        CASE WHEN random() < 0.08
             THEN round((amt * (0.3 + random() * 0.7))::numeric, 2)
             ELSE 0 END,
        round((amt * 0.09)::numeric, 2)
    FROM (
        SELECT round((20 + random() * 1980)::numeric, 2) AS amt
        FROM generate_series(1, 30000)
    ) s;

    RAISE NOTICE 'Seeded 100 stores, 5000 customers, 30000 orders.';
END
$seed$;

-- ── Products (small fixed catalogue; embeddings added by seed_embeddings.py) ──
INSERT INTO products (name, description, price)
SELECT * FROM (VALUES
    ('Trail Blazer Hiking Boots', 'Waterproof leather hiking boots with aggressive tread for rugged mountain trails.', 159.00),
    ('Summit Down Jacket',        'Lightweight 800-fill down jacket that keeps you warm in freezing winter conditions.', 220.00),
    ('Riverside Tent 2P',         'Two-person waterproof backpacking tent with a quick-pitch aluminium pole set.', 189.00),
    ('Trailhead Daypack 30L',     'Durable 30-litre daypack with hydration sleeve for long day hikes.', 95.00),
    ('Cascade Rain Shell',        'Breathable waterproof rain jacket with sealed seams for wet weather.', 130.00),
    ('Glacier Water Bottle',      'Insulated stainless-steel bottle that keeps drinks cold for 24 hours.', 32.00),
    ('Campfire Cook Set',         'Compact nesting camping cookware set for backcountry meals.', 64.00),
    ('Nordic Wool Socks',         'Warm merino wool hiking socks for cold-weather comfort.', 24.00),
    ('Carbon Trekking Poles',     'Ultralight adjustable carbon-fibre trekking poles for steep terrain.', 110.00),
    ('Sunrise Sleeping Bag',      'Three-season mummy sleeping bag rated to -5C for cold nights.', 145.00),
    ('Rapid Dry Towel',           'Quick-drying microfibre travel towel that packs down small.', 22.00),
    ('Alpine Headlamp',           'Rechargeable 400-lumen headlamp for night hiking and camp chores.', 41.00),
    ('Boulder Climbing Shoes',    'Sticky-rubber rock climbing shoes for bouldering and sport routes.', 135.00),
    ('Meadow Picnic Blanket',     'Water-resistant fleece picnic blanket for parks and beaches.', 38.00),
    ('Harbor Kayak Paddle',       'Lightweight aluminium kayak paddle with drip rings.', 78.00),
    ('Tundra Insulated Gloves',   'Windproof insulated gloves for skiing and snow sports.', 49.00),
    ('Canyon Trail Runners',      'Breathable trail running shoes with grippy lugged soles.', 118.00),
    ('Lakeside Camp Chair',       'Foldable lightweight camp chair with a cup holder.', 54.00),
    ('Forest Hammock',            'Packable double parachute-nylon hammock with tree straps.', 46.00),
    ('Polar Cooler 25L',          'Hard-sided 25-litre cooler that holds ice for three days.', 92.00),
    ('Drizzle Rain Pants',        'Packable waterproof over-trousers for hiking in the rain.', 69.00),
    ('Ember Camp Stove',          'Compact single-burner backpacking stove with piezo ignition.', 58.00),
    ('Ridge Sun Hat',             'Wide-brim UPF 50 sun hat for hot-weather hikes.', 29.00),
    ('Coastal Dry Bag 20L',       'Roll-top waterproof dry bag for kayaking and beach trips.', 34.00)
) AS v(name, description, price)
WHERE NOT EXISTS (SELECT 1 FROM products);
