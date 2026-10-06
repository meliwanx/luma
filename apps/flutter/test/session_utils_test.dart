import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/session_utils.dart';

void main() {
  final now = DateTime(2026, 10, 4, 15, 30);

  test('formats timestamps from the current minute through hours', () {
    expect(formatRelativeTime(now, now: now), '刚刚');
    expect(
      formatRelativeTime(now.subtract(const Duration(seconds: 59)), now: now),
      '刚刚',
    );
    expect(
      formatRelativeTime(now.subtract(const Duration(minutes: 1)), now: now),
      '1分钟前',
    );
    expect(
      formatRelativeTime(now.subtract(const Duration(minutes: 59)), now: now),
      '59分钟前',
    );
    expect(
      formatRelativeTime(now.subtract(const Duration(hours: 1)), now: now),
      '1小时前',
    );
    expect(
      formatRelativeTime(now.subtract(const Duration(hours: 12)), now: now),
      '12小时前',
    );
  });

  test('uses calendar boundaries for yesterday and previous days', () {
    expect(formatRelativeTime(DateTime(2026, 10, 3, 23, 55), now: now), '昨天');
    expect(formatRelativeTime(DateTime(2026, 10, 2, 15, 30), now: now), '2天前');
    expect(formatRelativeTime(DateTime(2026, 9, 29, 15, 30), now: now), '5天前');
  });

  test('falls back to month and day for older timestamps', () {
    expect(
      formatRelativeTime(DateTime(2026, 9, 26, 15, 30), now: now),
      '9月26日',
    );
    expect(formatRelativeTime(DateTime(2025, 12, 8, 10), now: now), '12月8日');
  });

  test('treats a future timestamp as just now', () {
    expect(relativeTime(now.add(const Duration(minutes: 5)), now: now), '刚刚');
  });
}
