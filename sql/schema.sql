-- Apply only after checking the actual course database and table names.
-- Operation status and reservation insert must commit in one transaction.

CREATE TABLE IF NOT EXISTS agent_operations (
    request_id CHAR(36) NOT NULL PRIMARY KEY,
    thread_id VARCHAR(128) NOT NULL,
    operation_type VARCHAR(32) NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    payload_json JSON NOT NULL,
    status ENUM('PENDING', 'SUCCEEDED', 'REJECTED', 'FAILED', 'NEEDS_REVIEW')
        NOT NULL DEFAULT 'PENDING',
    reservation_id BIGINT UNSIGNED NULL,
    error_code VARCHAR(64) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    KEY ix_agent_operations_thread_status (thread_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reservations (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    request_id CHAR(36) NOT NULL,
    thread_id VARCHAR(128) NOT NULL,
    num_people INT UNSIGNED NOT NULL,
    num_children INT UNSIGNED NOT NULL DEFAULT 0,
    arrival_time DATETIME NOT NULL,
    customer_name VARCHAR(80) NOT NULL DEFAULT '来宾',
    phone VARCHAR(32) NOT NULL DEFAULT '',
    seat_preference VARCHAR(128) NOT NULL,
    main_dish_preference VARCHAR(512) NOT NULL,
    other_comments TEXT NULL,
    status ENUM('CONFIRMED', 'CANCELLED') NOT NULL DEFAULT 'CONFIRMED',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_reservations_request_id (request_id),
    KEY ix_reservations_arrival_status (arrival_time, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS menu_items (
    dish_name VARCHAR(128) NOT NULL PRIMARY KEY,
    price DECIMAL(10, 2) NOT NULL,
    description TEXT NOT NULL,
    category VARCHAR(64) NOT NULL,
    spice_level VARCHAR(32) NOT NULL DEFAULT '不辣',
    flavor VARCHAR(128) NOT NULL,
    main_ingredients VARCHAR(512) NOT NULL DEFAULT '',
    cooking_method VARCHAR(128) NOT NULL DEFAULT '',
    is_vegetarian TINYINT(1) NOT NULL DEFAULT 0,
    allergens VARCHAR(512) NOT NULL DEFAULT '',
    is_available TINYINT(1) NOT NULL DEFAULT 1,
    is_featured TINYINT(1) NOT NULL DEFAULT 0,
    KEY ix_menu_category_available (category, is_available),
    KEY ix_menu_featured_available (is_featured, is_available)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO menu_items
    (dish_name, price, description, category, spice_level, flavor,
     main_ingredients, cooking_method, is_vegetarian, allergens, is_available, is_featured)
VALUES
    ('小笼包', 28.00, '皮薄馅鲜，搭配姜丝与香醋。', '点心', '不辣', '鲜香、咸鲜', '猪肉、小麦', '蒸', 0, '猪肉、小麦', 1, 1),
    ('清蒸鲈鱼', 88.00, '鲜嫩鲈鱼清蒸，葱姜提鲜。', '热菜', '不辣', '清淡、咸鲜', '鲈鱼、葱、姜', '清蒸', 0, '鱼类', 1, 1),
    ('本帮红烧肉', 68.00, '浓油赤酱，软糯入味。', '热菜', '不辣', '咸甜、浓郁', '猪肉', '红烧', 0, '猪肉', 1, 1),
    ('油爆虾', 78.00, '上海本帮做法，外脆里嫩。', '热菜', '微辣', '鲜香、微甜', '河虾', '油爆', 0, '虾类', 1, 1),
    ('葱油拌面', 22.00, '葱香浓郁，口味清爽。', '主食', '不辣', '清淡、咸鲜', '小麦、香葱', '拌', 1, '小麦', 1, 0),
    ('荠菜馄饨', 32.00, '荠菜猪肉馅，清汤鲜美。', '主食', '不辣', '清淡、鲜香', '荠菜、猪肉、小麦', '煮', 0, '猪肉、小麦', 1, 0),
    ('上海熏鱼', 48.00, '先炸后浸，咸甜适口。', '凉菜', '不辣', '咸甜、浓郁', '青鱼', '炸、浸汁', 0, '鱼类', 1, 0),
    ('麻婆豆腐', 38.00, '豆腐细嫩，麻辣鲜香。', '热菜', '麻辣', '麻辣、香辣', '豆腐、牛肉', '烧', 0, '大豆', 1, 0),
    ('桂花酒酿圆子', 26.00, '酒酿清甜，带淡淡桂花香。', '甜品', '不辣', '甜口、清淡', '糯米、桂花', '煮', 1, '糯米', 1, 0),
    ('蟹粉豆腐', 58.00, '蟹粉鲜香，豆腐细嫩。', '热菜', '不辣', '鲜香、咸鲜', '蟹肉、豆腐', '烩', 0, '蟹类、大豆', 0, 0)
ON DUPLICATE KEY UPDATE
    price = VALUES(price), description = VALUES(description), category = VALUES(category),
    spice_level = VALUES(spice_level), flavor = VALUES(flavor),
    main_ingredients = VALUES(main_ingredients), cooking_method = VALUES(cooking_method),
    is_vegetarian = VALUES(is_vegetarian), allergens = VALUES(allergens),
    is_available = VALUES(is_available), is_featured = VALUES(is_featured);

CREATE TABLE IF NOT EXISTS agent_operation_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    request_id CHAR(36) NOT NULL,
    step VARCHAR(64) NOT NULL,
    attempt INT UNSIGNED NOT NULL DEFAULT 0,
    state VARCHAR(32) NOT NULL,
    error_code VARCHAR(64) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_operation_events_request_time (request_id, created_at),
    CONSTRAINT fk_operation_events_request
        FOREIGN KEY (request_id) REFERENCES agent_operations (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
