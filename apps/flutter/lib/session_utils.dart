// Helpers shared by the session switcher and its tests.

/// Formats a session timestamp in the compact form used by the session list.
///
/// [now] is injectable so callers and tests can use one consistent clock. Both
/// values are converted to the device's local timezone before comparing them;
/// API timestamps are normally ISO-8601 values with an explicit offset.
String formatRelativeTime(DateTime value, {DateTime? now}) {
  final current = (now ?? DateTime.now()).toLocal();
  final localValue = value.toLocal();
  final elapsed = current.difference(localValue);

  // A future timestamp can briefly occur when a server and device clocks are
  // a little out of sync. It is more useful to show it as recent than to
  // expose a negative duration to the user.
  if (elapsed.isNegative || elapsed.inSeconds < 60) return '刚刚';

  // Use UTC for the date-only values so daylight-saving transitions cannot
  // turn a one-calendar-day difference into a 23- or 25-hour duration.
  final currentDay = DateTime.utc(current.year, current.month, current.day);
  final valueDay = DateTime.utc(
    localValue.year,
    localValue.month,
    localValue.day,
  );
  final calendarDays = currentDay.difference(valueDay).inDays;

  // Minute/hour labels only apply while both timestamps are on today's
  // calendar day. A late-night message is therefore labelled "昨天" after
  // midnight, even if less than 24 hours have elapsed.
  if (calendarDays == 0) {
    if (elapsed.inMinutes < 60) return '${elapsed.inMinutes}分钟前';
    return '${elapsed.inHours}小时前';
  }
  if (calendarDays == 1) return '昨天';
  if (calendarDays > 1 && calendarDays < 7) return '$calendarDays天前';
  return '${localValue.month}月${localValue.day}日';
}

/// Short alias for use at call sites where the context already says this is a
/// relative time value.
String relativeTime(DateTime value, {DateTime? now}) =>
    formatRelativeTime(value, now: now);
