package com.example.luma_client

import android.app.Activity
import android.content.Intent
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var exportBytes: ByteArray? = null
    private var exportResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "luma/files")
            .setMethodCallHandler { call, result ->
                if (call.method != "export") {
                    result.notImplemented()
                } else if (exportResult != null) {
                    result.error("export_busy", "已有文件正在保存", null)
                } else {
                    val filename = call.argument<String>("filename")
                    val bytes = call.argument<ByteArray>("bytes")
                    if (filename == null || bytes == null) {
                        result.error("invalid_file", "文件无效", null)
                    } else {
                        exportBytes = bytes
                        exportResult = result
                        try {
                            startActivityForResult(Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
                                addCategory(Intent.CATEGORY_OPENABLE)
                                type = "application/octet-stream"
                                putExtra(Intent.EXTRA_TITLE, filename.substringAfterLast('/'))
                            }, 4101)
                        } catch (_: Exception) {
                            exportBytes = null
                            exportResult = null
                            result.error("export_failed", "文件保存失败", null)
                        }
                    }
                }
            }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != 4101) return
        val result = exportResult ?: return
        val bytes = exportBytes
        exportResult = null
        exportBytes = null
        val uri = data?.data
        if (resultCode != Activity.RESULT_OK || uri == null || bytes == null) {
            result.success(false)
            return
        }
        Thread {
            try {
                val output = contentResolver.openOutputStream(uri)
                    ?: throw IllegalStateException("No output stream")
                output.use { it.write(bytes) }
                runOnUiThread { result.success(true) }
            } catch (_: Exception) {
                runOnUiThread { result.error("export_failed", "文件保存失败", null) }
            }
        }.start()
    }
}
