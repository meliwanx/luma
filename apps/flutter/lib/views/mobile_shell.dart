import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../brand.dart';
import '../glass.dart';
import '../theme.dart';
import 'side_menu.dart';

export 'side_menu.dart' show mobileTabIcons, mobileTabLabels;

/// The pushed main screen keeps its layout while the drawer owns the keyboard.
class MobileKeyboardScope extends InheritedWidget {
  const MobileKeyboardScope({
    super.key,
    required this.ignoreInsets,
    required super.child,
  });

  final bool ignoreInsets;

  static bool ignoreInsetsOf(BuildContext context) =>
      context
          .dependOnInheritedWidgetOfExactType<MobileKeyboardScope>()
          ?.ignoreInsets ??
      false;

  @override
  bool updateShouldNotify(MobileKeyboardScope oldWidget) =>
      ignoreInsets != oldWidget.ignoreInsets;
}

class MobileShell extends StatefulWidget {
  const MobileShell({
    super.key,
    required this.tab,
    required this.title,
    required this.child,
    required this.sessions,
    required this.sessionId,
    required this.onTabChanged,
    required this.onMainChat,
    required this.onNewSession,
    required this.onSettings,
    required this.onSearch,
    required this.onSelectSession,
    required this.onDeleteSession,
    required this.onRenameSession,
    this.unreadCount = 0,
    this.onNotifications,
    this.mainSessionId,
    this.generatingSessionIds = const <String>{},
    this.completedSessionIds = const <String>{},
    this.onTabBarHeightChanged,
  });

  static const headerHeight = 112.0;
  static const headerTopInset = 8.0;
  static const headerLogoSize = 48.0;
  static const mainChatHeaderHeight = headerTopInset + headerLogoSize + 16.0;
  static const tabBarHeight = 64.0;
  static const tabBarGap = 8.0;
  static const horizontalInset = 16.0;

  final int tab;
  final String title;
  final Widget child;
  final List<Map<String, dynamic>> sessions;
  final String sessionId;
  final ValueChanged<int> onTabChanged;
  final VoidCallback onMainChat;
  final VoidCallback onNewSession;
  final VoidCallback onSettings;
  final VoidCallback onSearch;
  final ValueChanged<String> onSelectSession;
  final Future<bool> Function(Map<String, dynamic>) onDeleteSession;
  final Future<bool> Function(Map<String, dynamic>) onRenameSession;
  final int unreadCount;
  final VoidCallback? onNotifications;
  final String? mainSessionId;
  final Set<String> generatingSessionIds;
  final Set<String> completedSessionIds;
  final ValueChanged<double>? onTabBarHeightChanged;

  static bool isSideSession({
    required String sessionId,
    required List<Map<String, dynamic>> sessions,
    String? mainSessionId,
  }) =>
      (mainSessionId != null && sessionId != mainSessionId) ||
      sessions.any(
        (session) => session['id'] == sessionId && session['kind'] == 'side',
      );

  @override
  State<MobileShell> createState() => _MobileShellState();
}

class _MobileShellState extends State<MobileShell>
    with SingleTickerProviderStateMixin {
  late final AnimationController _menuAnimation;
  late final Animation<double> _menuProgress;
  bool _menuOpen = false;
  double _dragDistance = 0;
  final _tabBarKey = GlobalKey();
  double? _reportedTabBarHeight;
  int _menuTransition = 0;

  bool get _isSideChat =>
      widget.tab == 0 &&
      MobileShell.isSideSession(
        sessionId: widget.sessionId,
        sessions: widget.sessions,
        mainSessionId: widget.mainSessionId,
      );

  bool get _isMainChat => widget.tab == 0 && !_isSideChat;

  bool get _mainCompleted =>
      widget.tab == 0 &&
      widget.mainSessionId != null &&
      widget.completedSessionIds.contains(widget.mainSessionId);

  void _dismissKeyboard() => FocusManager.instance.primaryFocus?.unfocus();

  void _handlePointerDown(PointerDownEvent event) {
    final focus = FocusManager.instance.primaryFocus;
    if (focus == null || !focus.hasFocus) return;
    BuildContext? inputContext = focus.context;
    inputContext?.visitAncestorElements((element) {
      if (element.widget is TextField) {
        inputContext = element;
        return false;
      }
      return true;
    });
    final box = inputContext?.findRenderObject();
    if (box is RenderBox && box.attached && box.hasSize) {
      final bounds = box.localToGlobal(Offset.zero) & box.size;
      if (bounds.contains(event.position)) return;
    }
    _dismissKeyboard();
  }

  void _reportTabBarHeight(bool keyboardVisible) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final box = _tabBarKey.currentContext?.findRenderObject();
      final height = !keyboardVisible && box is RenderBox && box.hasSize
          ? box.size.height
          : 0.0;
      if (_reportedTabBarHeight == height) return;
      _reportedTabBarHeight = height;
      widget.onTabBarHeightChanged?.call(height);
    });
  }

  @override
  void initState() {
    super.initState();
    _menuAnimation = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 300),
    );
    _menuProgress = CurvedAnimation(
      parent: _menuAnimation,
      curve: Curves.easeOutCubic,
      reverseCurve: Curves.easeInCubic,
    );
  }

  @override
  void dispose() {
    _menuAnimation.dispose();
    super.dispose();
  }

  Future<void> _setMenuOpen(bool open) async {
    if (_menuOpen == open) return;
    final keyboardVisible = MediaQuery.viewInsetsOf(context).bottom > 0;
    final transition = ++_menuTransition;
    _dismissKeyboard();
    HapticFeedback.selectionClick();
    setState(() => _menuOpen = open);
    if (open) {
      // Unfocus first, then let the keyboard begin its dismissal before pushing.
      if (keyboardVisible) {
        await Future<void>.delayed(const Duration(milliseconds: 80));
      }
      if (!mounted || transition != _menuTransition) return;
      _menuAnimation.forward();
    } else {
      _menuAnimation.reverse();
    }
  }

  void _selectTab(int index) {
    _dismissKeyboard();
    HapticFeedback.selectionClick();
    widget.onTabChanged(index);
    _setMenuOpen(false);
  }

  void _menuAction(VoidCallback action) {
    _setMenuOpen(false);
    action();
  }

  void _dragStart(DragStartDetails details) {
    _dismissKeyboard();
    _dragDistance = 0;
  }

  void _dragUpdate(DragUpdateDetails details) =>
      _dragDistance += details.delta.dx;

  void _dragEnd(DragEndDetails details) {
    final velocity = details.primaryVelocity ?? 0;
    if (_menuOpen && (_dragDistance < -35 || velocity < -350)) {
      _setMenuOpen(false);
    } else if (!_menuOpen && (_dragDistance > 35 || velocity > 350)) {
      _setMenuOpen(true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final dark = Theme.of(context).brightness == Brightness.dark;
    final background = dark ? Colors.black : Colors.white;
    final mediaQuery = MediaQuery.of(context);
    final keyboardVisible = mediaQuery.viewInsets.bottom > 0;
    return GestureDetector(
      behavior: HitTestBehavior.translucent,
      onTap: _dismissKeyboard,
      child: Listener(
        behavior: HitTestBehavior.translucent,
        onPointerDown: _handlePointerDown,
        child: AnimatedBuilder(
          animation: _menuAnimation,
          builder: (context, _) {
            final progress = _menuProgress.value;
            final drawerActive = _menuOpen || progress > 0;
            _reportTabBarHeight(keyboardVisible && !drawerActive);
            return PopScope(
              canPop: !drawerActive,
              onPopInvokedWithResult: (didPop, result) {
                if (!didPop) _setMenuOpen(false);
              },
              child: Scaffold(
                extendBody: true,
                resizeToAvoidBottomInset: !drawerActive,
                backgroundColor: background,
                body: Builder(
                  builder: (bodyContext) => MediaQuery(
                    // Keep the original inset when Scaffold resizes chat.
                    data: MediaQuery.of(bodyContext)
                        .copyWith(viewInsets: mediaQuery.viewInsets),
                    child: LayoutBuilder(
                      builder: (context, constraints) {
                        final width = constraints.maxWidth;
                        return Stack(
                          fit: StackFit.expand,
                          children: [
                            if (progress > 0)
                              Positioned(
                                left: 0,
                                top: 0,
                                bottom: 0,
                                width: width * .78,
                                child: ExcludeSemantics(
                                  excluding: !_menuOpen,
                                  child: IgnorePointer(
                                    ignoring: !_menuOpen,
                                    child: GestureDetector(
                                      behavior: HitTestBehavior.translucent,
                                      onHorizontalDragStart: _dragStart,
                                      onHorizontalDragUpdate: _dragUpdate,
                                      onHorizontalDragEnd: _dragEnd,
                                      child: SideMenu(
                                        tab: widget.tab,
                                        sessions: widget.sessions,
                                        sessionId: widget.sessionId,
                                        onTabChanged: _selectTab,
                                        onMainChat: () {
                                          _selectTab(0);
                                          widget.onMainChat();
                                        },
                                        onSettings: () =>
                                            _menuAction(widget.onSettings),
                                        onSearch: () =>
                                            _menuAction(widget.onSearch),
                                        onNewSession: () =>
                                            _menuAction(widget.onNewSession),
                                        onSelectSession: (id) => _menuAction(
                                          () => widget.onSelectSession(id),
                                        ),
                                        onDeleteSession: widget.onDeleteSession,
                                        onRenameSession: widget.onRenameSession,
                                        mainSessionId: widget.mainSessionId,
                                        generatingSessionIds:
                                            widget.generatingSessionIds,
                                        completedSessionIds:
                                            widget.completedSessionIds,
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                            Transform.translate(
                              offset: Offset(width * .78 * progress, 0),
                              child: Transform.scale(
                                scale: 1 - .12 * progress,
                                alignment: Alignment.centerLeft,
                                child: ClipRRect(
                                  borderRadius: BorderRadius.circular(
                                    32 * progress,
                                  ),
                                  child: ExcludeSemantics(
                                    excluding: drawerActive,
                                    child: ExcludeFocus(
                                      excluding: drawerActive,
                                      child: IgnorePointer(
                                        ignoring: drawerActive,
                                        child: MobileKeyboardScope(
                                          ignoreInsets: drawerActive,
                                          child: MediaQuery.removeViewInsets(
                                            context: context,
                                            removeBottom: drawerActive,
                                            child: ColoredBox(
                                              color: background,
                                              child: Stack(
                                                fit: StackFit.expand,
                                                children: [
                                                  AnimatedSwitcher(
                                                    layoutBuilder:
                                                        (
                                                          currentChild,
                                                          previousChildren,
                                                        ) =>
                                                            currentChild ??
                                                            const SizedBox.shrink(),
                                                    duration: const Duration(
                                                      milliseconds: 220,
                                                    ),
                                                    switchInCurve:
                                                        Curves.easeOut,
                                                    switchOutCurve:
                                                        Curves.easeIn,
                                                    transitionBuilder:
                                                        (
                                                          child,
                                                          animation,
                                                        ) => FadeTransition(
                                                          opacity: animation,
                                                          child: SlideTransition(
                                                            position:
                                                                Tween<Offset>(
                                                                  begin:
                                                                      const Offset(
                                                                        0,
                                                                        .015,
                                                                      ),
                                                                  end: Offset
                                                                      .zero,
                                                                ).animate(
                                                                  animation,
                                                                ),
                                                            child: child,
                                                          ),
                                                        ),
                                                    child: KeyedSubtree(
                                                      key: ValueKey(widget.tab),
                                                      child: widget.child,
                                                    ),
                                                  ),
                                                  Positioned(
                                                    top:
                                                        mediaQuery.padding.top +
                                                        MobileShell
                                                            .headerTopInset,
                                                    left: 16,
                                                    right: 16,
                                                    child: _header(context),
                                                  ),
                                                  if (!keyboardVisible ||
                                                      drawerActive)
                                                    Positioned(
                                                      left: MobileShell
                                                          .horizontalInset,
                                                      right: MobileShell
                                                          .horizontalInset,
                                                      bottom:
                                                          mediaQuery
                                                              .padding
                                                              .bottom +
                                                          MobileShell.tabBarGap,
                                                      child: SizedBox(
                                                        key: _tabBarKey,
                                                        child: _tabBar(context),
                                                      ),
                                                    ),
                                                  if (progress > 0)
                                                    IgnorePointer(
                                                      child: ColoredBox(
                                                        color: Colors.black
                                                            .withValues(
                                                              alpha:
                                                                  .28 *
                                                                  progress,
                                                            ),
                                                      ),
                                                    ),
                                                ],
                                              ),
                                            ),
                                          ),
                                        ),
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                            ),
                            if (progress > 0)
                              Positioned(
                                key: const ValueKey('mobile-menu-scrim'),
                                left: width * .78 * progress,
                                top: 0,
                                right: 0,
                                bottom: 0,
                                child: GestureDetector(
                                  behavior: HitTestBehavior.opaque,
                                  onTap: () => _setMenuOpen(false),
                                  onHorizontalDragStart: _dragStart,
                                  onHorizontalDragUpdate: _dragUpdate,
                                  onHorizontalDragEnd: _dragEnd,
                                ),
                              ),
                            if (!drawerActive)
                              Positioned(
                                key: const ValueKey('mobile-menu-edge'),
                                left: 0,
                                top: 0,
                                bottom: 0,
                                width: 20,
                                child: GestureDetector(
                                  behavior: HitTestBehavior.translucent,
                                  onHorizontalDragStart: _dragStart,
                                  onHorizontalDragUpdate: _dragUpdate,
                                  onHorizontalDragEnd: _dragEnd,
                                ),
                              ),
                          ],
                        );
                      },
                    ),
                  ),
                ),
              ),
            );
          },
        ),
      ),
    );
  }

  Widget _header(BuildContext context) {
    final colors = context.muse;
    final logo = Stack(
      children: [
        const GlassSurface(
          shape: GlassShape.circle,
          child: SizedBox(
            width: MobileShell.headerLogoSize,
            height: MobileShell.headerLogoSize,
            child: Center(
              child: LumaLogo(key: ValueKey('mobile-header-logo'), width: 38),
            ),
          ),
        ),
        if (_isMainChat && _mainCompleted)
          Positioned(top: 0, right: 0, child: _completedIndicator(context)),
      ],
    );
    return Stack(
      alignment: Alignment.topCenter,
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 66),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (widget.tab == 0) ...[
                _isMainChat ? _menuButton(logo) : logo,
                if (_isSideChat) const SizedBox(height: 7),
              ] else
                const SizedBox(height: 6),
              if (!_isMainChat)
                Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Flexible(
                      child: _menuButton(
                        GlassSurface(
                          shape: GlassShape.capsule,
                          padding: const EdgeInsets.symmetric(
                            horizontal: 15,
                            vertical: 8,
                          ),
                          child: Text(
                            widget.title,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: colors.text,
                              fontSize: 13,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        ),
                      ),
                    ),
                    if (_mainCompleted) ...[
                      const SizedBox(width: 7),
                      _completedIndicator(context),
                    ],
                  ],
                ),
            ],
          ),
        ),
        Align(
          alignment: Alignment.topLeft,
          child: GlassSurface(
            shape: GlassShape.circle,
            child: IconButton(
              key: ValueKey(
                _isSideChat ? 'mobile-main-chat-button' : 'mobile-menu-button',
              ),
              tooltip: _isSideChat ? '返回主聊天' : '菜单',
              onPressed: () {
                if (_isSideChat) {
                  _dismissKeyboard();
                  HapticFeedback.selectionClick();
                  widget.onMainChat();
                } else {
                  _setMenuOpen(true);
                }
              },
              constraints: const BoxConstraints.tightFor(
                width: MobileShell.headerLogoSize,
                height: MobileShell.headerLogoSize,
              ),
              icon: Icon(
                _isSideChat ? Icons.arrow_back_rounded : Icons.menu_rounded,
                color: colors.text,
                size: 23,
              ),
            ),
          ),
        ),
      ],
    );
  }

  Widget _menuButton(Widget child) => Semantics(
    button: true,
    label: '打开会话菜单',
    child: GestureDetector(
      key: const ValueKey('mobile-title-button'),
      behavior: HitTestBehavior.opaque,
      onTap: () => _setMenuOpen(true),
      child: child,
    ),
  );

  Widget _completedIndicator(BuildContext context) => Semantics(
    label: '主聊天回复已完成',
    child: Container(
      key: const ValueKey('mobile-main-completed'),
      width: 8,
      height: 8,
      decoration: BoxDecoration(
        color: context.muse.accent,
        shape: BoxShape.circle,
      ),
    ),
  );

  Widget _tabBar(BuildContext context) {
    final colors = context.muse;
    final dark = Theme.of(context).brightness == Brightness.dark;
    return GlassSurface(
      key: const ValueKey('mobile-tab-bar'),
      shape: GlassShape.capsule,
      padding: const EdgeInsets.all(6),
      child: SizedBox(
        height: MobileShell.tabBarHeight - 12,
        child: LayoutBuilder(
          builder: (context, constraints) {
            final itemWidth = constraints.maxWidth / mobileTabLabels.length;
            return Stack(
              children: [
                AnimatedPositioned(
                  duration: const Duration(milliseconds: 250),
                  curve: Curves.easeOutCubic,
                  top: 0,
                  bottom: 0,
                  left: itemWidth * widget.tab,
                  width: itemWidth,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: dark ? .18 : .80),
                      borderRadius: BorderRadius.circular(999),
                      border: Border.all(
                        color: Colors.white.withValues(alpha: dark ? .10 : .95),
                      ),
                    ),
                  ),
                ),
                Row(
                  children: [
                    for (var index = 0; index < mobileTabLabels.length; index++)
                      Expanded(
                        child: Semantics(
                          selected: widget.tab == index,
                          button: true,
                          label: mobileTabLabels[index],
                          child: Tooltip(
                            message: mobileTabLabels[index],
                            child: InkWell(
                              key: ValueKey('mobile-tab-$index'),
                              borderRadius: BorderRadius.circular(999),
                              onTap: () => _selectTab(index),
                              child: SizedBox.expand(
                                child: Icon(
                                  mobileTabIcons[index],
                                  size: 24,
                                  color: widget.tab == index
                                      ? colors.text
                                      : colors.muted,
                                ),
                              ),
                            ),
                          ),
                        ),
                      ),
                  ],
                ),
              ],
            );
          },
        ),
      ),
    );
  }
}
