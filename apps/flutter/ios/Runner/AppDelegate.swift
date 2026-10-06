import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    let channel = FlutterMethodChannel(
      name: "luma/files", binaryMessenger: engineBridge.applicationRegistrar.messenger())
    channel.setMethodCallHandler { call, result in
      guard call.method == "export" else {
        result(FlutterMethodNotImplemented)
        return
      }
      guard let args = call.arguments as? [String: Any],
            let filename = args["filename"] as? String,
            let bytes = args["bytes"] as? FlutterStandardTypedData else {
        result(FlutterError(code: "invalid_file", message: "文件无效", details: nil))
        return
      }
      let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString, isDirectory: true)
      let file = directory.appendingPathComponent((filename as NSString).lastPathComponent)
      do {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try bytes.data.write(to: file, options: .atomic)
        guard let scene = UIApplication.shared.connectedScenes
          .compactMap({ $0 as? UIWindowScene }).first(where: { $0.activationState == .foregroundActive }),
              var presenter = scene.windows.first(where: { $0.isKeyWindow })?.rootViewController else {
          try? FileManager.default.removeItem(at: directory)
          result(FlutterError(code: "no_window", message: "窗口不可用", details: nil))
          return
        }
        while let presented = presenter.presentedViewController { presenter = presented }
        let share = UIActivityViewController(activityItems: [file], applicationActivities: nil)
        share.popoverPresentationController?.sourceView = presenter.view
        share.popoverPresentationController?.sourceRect = CGRect(
          x: presenter.view.bounds.midX, y: presenter.view.bounds.midY, width: 1, height: 1)
        share.completionWithItemsHandler = { _, completed, _, error in
          try? FileManager.default.removeItem(at: directory)
          if error != nil {
            result(FlutterError(code: "export_failed", message: "文件保存失败", details: nil))
          } else { result(completed) }
        }
        presenter.present(share, animated: true)
      } catch {
        try? FileManager.default.removeItem(at: directory)
        result(FlutterError(code: "export_failed", message: "文件保存失败", details: nil))
      }
    }
  }
}
