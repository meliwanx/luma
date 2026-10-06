import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/views/notifications.dart';

void main() {
  test(
    'notification merge retains a stream arrival beyond a 100-item snapshot',
    () {
      final start = DateTime.utc(2026, 10, 4);
      final snapshot = List<Map<String, dynamic>>.generate(
        100,
        (index) => {
          'id': 'notice-$index',
          'created_at': start.add(Duration(minutes: index)).toIso8601String(),
        },
      );
      final current = <Map<String, dynamic>>[
        {
          'id': 'stream-arrival',
          'created_at': start
              .add(const Duration(minutes: 100))
              .toIso8601String(),
        },
      ];

      final merged = mergeNotifications(current, snapshot);

      expect(merged, hasLength(100));
      expect(merged.first['id'], 'stream-arrival');
      expect(merged.last['id'], 'notice-1');
      expect(merged.any((item) => item['id'] == 'notice-0'), isFalse);
    },
  );

  test('a stale unread snapshot does not undo an acknowledged local read', () {
    final merged = mergeNotifications(
      [
        {'id': 'notice-1', 'read_at': '2026-10-04T01:00:00Z'},
      ],
      [
        {'id': 'notice-1', 'read_at': null, 'title': '更新后的标题'},
      ],
    );

    expect(merged, hasLength(1));
    expect(merged.single['read_at'], '2026-10-04T01:00:00Z');
    expect(merged.single['title'], '更新后的标题');
  });

  test('a server read updates an unread local item', () {
    final merged = mergeNotifications(
      [
        {'id': 'notice-1', 'read_at': null},
      ],
      [
        {'id': 'notice-1', 'read_at': '2026-10-04T02:00:00Z'},
      ],
    );

    expect(merged.single['read_at'], '2026-10-04T02:00:00Z');
  });

  test(
    'partial updates preserve stored fields and deduplicate notification ids',
    () {
      final merged = mergeNotifications(
        [
          {'id': 'notice-1', 'title': '提醒', 'body': '原有内容'},
        ],
        [
          {'id': 'notice-1', 'title': '新提醒'},
          {'id': 'notice-1', 'title': '重复记录'},
        ],
      );

      expect(merged, hasLength(1));
      expect(merged.single['title'], '新提醒');
      expect(merged.single['body'], '原有内容');
    },
  );

  test(
    'invalid ids are ignored and equal timestamps keep deterministic order',
    () {
      final merged = mergeNotifications(
        [
          {'id': 'current-1', 'created_at': '2026-10-04T00:00:00Z'},
        ],
        [
          {'id': '', 'created_at': '2026-10-05T00:00:00Z'},
          {'id': null},
          {'id': 12},
          {'id': 'incoming-1', 'created_at': '2026-10-04T00:00:00Z'},
          {'id': 'incoming-2', 'created_at': '2026-10-04T00:00:00Z'},
          {'id': 'no-timestamp'},
        ],
      );

      expect(merged.map((item) => item['id']), [
        'incoming-1',
        'incoming-2',
        'current-1',
        'no-timestamp',
      ]);
    },
  );

  test('notification merge does not mutate either source list or map', () {
    final current = <Map<String, dynamic>>[
      {'id': 'notice-1', 'read_at': '2026-10-04T01:00:00Z'},
    ];
    final incoming = <Map<String, dynamic>>[
      {'id': 'notice-1', 'read_at': null},
    ];

    final merged = mergeNotifications(current, incoming);
    merged.single['title'] = '修改合并结果';

    expect(current.single, {
      'id': 'notice-1',
      'read_at': '2026-10-04T01:00:00Z',
    });
    expect(incoming.single, {'id': 'notice-1', 'read_at': null});
  });
}
