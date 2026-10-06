import 'package:flutter/material.dart';

IconData contentIcon(dynamic value) {
  switch (value) {
    case 'chart':
      return Icons.bar_chart_rounded;
    case 'doc':
      return Icons.description_outlined;
    case 'search':
      return Icons.search_rounded;
    case 'calendar':
      return Icons.calendar_today_outlined;
    case 'code':
      return Icons.code_rounded;
    case 'mail':
      return Icons.mail_outline_rounded;
    case 'book':
      return Icons.menu_book_outlined;
    case 'money':
      return Icons.account_balance_wallet_outlined;
    case 'heart':
      return Icons.favorite_border_rounded;
    case 'globe':
      return Icons.public_rounded;
    case 'checklist':
      return Icons.checklist_rounded;
    case 'spark':
    default:
      return Icons.auto_awesome_outlined;
  }
}

String contentRelativeTime(dynamic value) {
  final date = DateTime.tryParse('$value');
  if (date == null) return '';
  final elapsed = DateTime.now().difference(date);
  if (elapsed.inMinutes < 1) return '刚刚';
  if (elapsed.inHours < 1) return '${elapsed.inMinutes} 分钟前';
  if (elapsed.inDays < 1) return '${elapsed.inHours} 小时前';
  if (elapsed.inDays < 7) return '${elapsed.inDays} 天前';
  final local = date.toLocal();
  return '${local.year}/${local.month}/${local.day}';
}

List<Map<String, dynamic>> contentMaps(dynamic value) => value is List
    ? value
          .whereType<Map>()
          .map((item) => Map<String, dynamic>.from(item))
          .toList()
    : [];
