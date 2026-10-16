-- MySQL 8 / InnoDB schema used by restaurant_app/database.py.
-- Create the database first (for example: CREATE DATABASE menu CHARACTER SET utf8mb4).

CREATE TABLE IF NOT EXISTS menu_items (
    dish_name VARCHAR(100) NOT NULL PRIMARY KEY,
    category VARCHAR(50) NOT NULL,
    price DECIMAL(8, 2) NOT NULL,
    description TEXT NOT NULL,
    flavors_json TEXT NOT NULL,
    is_featured TINYINT(1) NOT NULL DEFAULT 0,
    is_available TINYINT(1) NOT NULL DEFAULT 1,
    allergens VARCHAR(500) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_menu_category_available (category, is_available),
    KEY ix_menu_featured_available (is_featured, is_available)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_operations (
    request_id VARCHAR(80) NOT NULL PRIMARY KEY,
    thread_id VARCHAR(128) NOT NULL,
    operation_type VARCHAR(48) NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    payload_json TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    reservation_id VARCHAR(40) NULL,
    error_code VARCHAR(80) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_agent_operations_thread_status (thread_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id VARCHAR(40) NOT NULL PRIMARY KEY,
    request_id VARCHAR(80) NOT NULL UNIQUE,
    thread_id VARCHAR(128) NOT NULL,
    customer_name VARCHAR(80) NOT NULL,
    phone VARCHAR(32) NOT NULL,
    num_people INT NOT NULL,
    num_children INT NOT NULL DEFAULT 0,
    arrival_time DATETIME NOT NULL,
    seat_preference VARCHAR(128) NOT NULL,
    main_dish_preference VARCHAR(512) NOT NULL,
    other_comments TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'CONFIRMED',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_reservations_operation
        FOREIGN KEY (request_id) REFERENCES agent_operations (request_id),
    KEY ix_reservations_phone_created (phone, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_operation_events (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    request_id VARCHAR(80) NOT NULL,
    step VARCHAR(48) NOT NULL,
    attempt INT NOT NULL DEFAULT 0,
    state VARCHAR(20) NOT NULL,
    error_code VARCHAR(80) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_agent_operation_events_request
        FOREIGN KEY (request_id) REFERENCES agent_operations (request_id),
    KEY ix_operation_events_request_time (request_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id VARCHAR(128) NOT NULL PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO menu_items
    (dish_name, category, price, description, flavors_json, is_featured, is_available, allergens)
VALUES
    ('小笼包', '点心', 28.00, '皮薄馅鲜，搭配姜丝与香醋。', '["鲜香", "咸鲜"]', 1, 1, '含猪肉、含小麦'),
    ('清蒸鲈鱼', '热菜', 88.00, '鲜嫩鲈鱼清蒸，葱姜提鲜。', '["清淡", "咸鲜"]', 1, 1, '含鱼类'),
    ('本帮红烧肉', '热菜', 68.00, '浓油赤酱，软糯入味。', '["咸甜", "浓郁"]', 1, 1, '含猪肉'),
    ('油爆虾', '热菜', 78.00, '上海本帮做法，外脆里嫩。', '["鲜香", "微甜"]', 1, 1, '含虾类'),
    ('葱油拌面', '主食', 22.00, '葱香浓郁，口味清爽。', '["清淡", "咸鲜"]', 0, 1, '含小麦'),
    ('荠菜馄饨', '主食', 32.00, '荠菜猪肉馅，清汤鲜美。', '["清淡", "鲜香"]', 0, 1, '含猪肉、含小麦'),
    ('上海熏鱼', '凉菜', 48.00, '先炸后浸，咸甜适口。', '["咸甜", "浓郁"]', 0, 1, '含鱼类'),
    ('麻婆豆腐', '热菜', 38.00, '豆腐细嫩，麻辣鲜香。', '["麻辣", "香辣"]', 0, 1, '含大豆'),
    ('桂花酒酿圆子', '甜品', 26.00, '酒酿清甜，带淡淡桂花香。', '["甜口", "清淡"]', 0, 1, '含糯米'),
    ('蟹粉豆腐', '热菜', 58.00, '蟹粉鲜香，豆腐细嫩。', '["鲜香", "咸鲜"]', 0, 0, '含蟹类、含大豆')
ON DUPLICATE KEY UPDATE
    category = VALUES(category),
    price = VALUES(price),
    description = VALUES(description),
    flavors_json = VALUES(flavors_json),
    is_featured = VALUES(is_featured),
    is_available = VALUES(is_available),
    allergens = VALUES(allergens);
