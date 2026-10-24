package com.xiaohe.companion

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.MediaStore
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.view.WindowCompat
import com.chaquo.python.Kwarg
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.util.concurrent.atomic.AtomicBoolean

/**
 * "小禾" 全屏独立 App 主入口。
 *
 * 启动流程：
 *   1. 隐藏系统栏（沉浸式全面屏，适配 OPPO ColorOS 17）
 *   2. 检查 RECORD_AUDIO / POST_NOTIFICATIONS 权限
 *   3. Python.start(AndroidPlatform) 初始化 Chaquopy 运行时
 *   4. 后台线程启动 uvicorn（127.0.0.1:8000）
 *   5. 等 2s 让 Python 启动 → WebView.loadUrl("http://127.0.0.1:8000")
 *
 * 退出：WebView.canGoBack → goBack，否则 System.exit(0)
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView

    companion object {
        /** Process-level guard: uvicorn must only start once per process. */
        private val backendStarted = AtomicBoolean(false)
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { /* 结果在 onCreate 里预检查过 */ }

    // WebView 的文件选择回调：Android WebView 默认不处理 <input type="file">，
    // 必须由 onShowFileChooser 自己接管，否则相册/拍照/视频/头像按钮全部没反应。
    private var filePathCallback: ValueCallback<Array<Uri>>? = null

    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val cb = filePathCallback
        filePathCallback = null
        if (cb == null) return@registerForActivityResult

        var uris: Array<Uri>? = null
        if (result.resultCode == android.app.Activity.RESULT_OK) {
            val data = result.data
            val clip = data?.clipData
            uris = when {
                clip != null && clip.itemCount > 0 ->
                    Array(clip.itemCount) { i -> clip.getItemAt(i).uri }
                data?.data != null -> arrayOf(data.data!!)
                else -> null
            }
        }
        // 用户取消时也必须回传 null，否则 WebView 会一直挂住，之后再点毫无反应
        cb.onReceiveValue(uris)
    }

    // 原生拍照：相机 App 把照片直接写进我们给的 content:// URI
    private var cameraOutputUri: Uri? = null

    private val cameraLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val cb = filePathCallback
        filePathCallback = null
        val uri = cameraOutputUri
        cameraOutputUri = null
        if (cb == null) return@registerForActivityResult
        if (result.resultCode != android.app.Activity.RESULT_OK) {
            cb.onReceiveValue(null)
            return@registerForActivityResult
        }
        // 少数相机把缩略图塞在 data 里；正常情况直接用 EXTRA_OUTPUT 的 URI
        val finalUri = result.data?.data ?: uri
        cb.onReceiveValue(if (finalUri != null) arrayOf(finalUri) else null)
    }

    /** 建一个 ACTION_IMAGE_CAPTURE Intent；没有相机 App 时返回 null 由调用方回退。 */
    private fun buildCameraIntent(): Intent? = try {
        val dir = java.io.File(cacheDir, "captures").apply { mkdirs() }
        val file = java.io.File.createTempFile("shot_", ".jpg", dir)
        val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
        cameraOutputUri = uri
        Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
            putExtra(MediaStore.EXTRA_OUTPUT, uri)
            addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    } catch (e: Exception) {
        null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // ------------------------------------------------------------------
        // 1. OPPO ColorOS 17 全面屏 + 手势导航 + 刘海屏适配
        // ------------------------------------------------------------------
        WindowCompat.setDecorFitsSystemWindows(window, false)
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )

        // 沉浸式状态栏
        @Suppress("DEPRECATION")
        window.setStatusBarColor(android.graphics.Color.TRANSPARENT)
        window.navigationBarColor = android.graphics.Color.TRANSPARENT

        // ------------------------------------------------------------------
        // 2. 权限检查
        // ------------------------------------------------------------------
        val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms += Manifest.permission.POST_NOTIFICATIONS
        }
        val need = perms.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (need.isNotEmpty()) permissionLauncher.launch(need.toTypedArray())

        // ------------------------------------------------------------------
        // 3. 初始化 Chaquopy + 启动 Python 后端
        // ------------------------------------------------------------------
        // 进程级守卫：Activity 重建时不再重复启动 uvicorn（端口冲突）
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        // 设置 Python 运行时的 ANDROID_DATA_DIR（让 database.py 把 DB 放到 App 私有目录）
        // compareAndSet 保证整个进程内只执行一次，Activity 重建不会端口冲突
        if (backendStarted.compareAndSet(false, true)) {
            Thread {
                try {
                    val py = Python.getInstance()
                    val os = py.getModule("os")
                    // os.environ 是 mapping 对象、不可调用；必须用 __setitem__ 写入，
                    // 直接 callAttr("environ", ...) 会抛 TypeError 导致后端线程崩溃。
                    val environ = os.get("environ")!!
                    environ.callAttr("__setitem__", "ANDROID_DATA_DIR", filesDir.absolutePath)

                    val sys = py.getModule("sys")
                    sys.get("path")!!.callAttr("append", filesDir.absolutePath)

                    val uvicorn = py.getModule("uvicorn")
                    val appModule = py.getModule("app.main")
                    val appObj = appModule.get("app")

                    // Chaquopy 关键字参数必须用 Kwarg()，不能用 Kotlin Pair("host" to ...)
                    uvicorn.callAttr(
                        "run", appObj,
                        Kwarg("host", "127.0.0.1"),
                        Kwarg("port", 8000),
                        Kwarg("log_level", "info"),
                        Kwarg("access_log", false)
                    )
                } catch (e: Exception) {
                    backendStarted.set(false)  // 允许下次重建时重试
                    runOnUiThread {
                        Toast.makeText(
                            this,
                            "Python backend crashed: ${e.message}",
                            Toast.LENGTH_LONG
                        ).show()
                    }
                }
            }.start()
        }

        // ------------------------------------------------------------------
        // 4. 等 Python 启动 → 加载 WebView
        // ------------------------------------------------------------------
        Thread {
            // 给 Python + APScheduler 足够时间初始化
            Thread.sleep(2500)

            // 健康检查 —— 确保 uvicorn 已监听
            var retries = 10
            while (retries > 0) {
                try {
                    val url = java.net.URL("http://127.0.0.1:8000/")
                    val conn = url.openConnection() as java.net.HttpURLConnection
                    conn.connectTimeout = 500
                    conn.requestMethod = "GET"
                    if (conn.responseCode == 200) break
                } catch (_: Exception) { /* 等下一轮 */ }
                Thread.sleep(300)
                retries--
            }

            runOnUiThread {
                // 读取后端生成的访问令牌，注入 URL 实现自动登录
                val token = readAuthToken()
                val url = if (token.isNotEmpty()) {
                    "http://127.0.0.1:8000/?token=$token"
                } else {
                    "http://127.0.0.1:8000/"
                }
                setupWebView(url)
            }
        }.start()
    }

    // ----------------------------------------------------------------------
    // 从 App 私有目录读取 .auth_token（由 Python 端 security.py 生成）
    private fun readAuthToken(): String {
        return try {
            val f = java.io.File(filesDir, ".auth_token")
            if (f.exists()) f.readText(Charsets.UTF_8).trim() else ""
        } catch (_: Exception) {
            ""
        }
    }

    // ----------------------------------------------------------------------
    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView(url: String) {
        webView = findViewById(R.id.web_view)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            databaseEnabled = true
            allowFileAccess = true
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            // 解决 OPPO 某些机型 WebView 内核过旧导致 SSE 断流
            loadWithOverviewMode = true
            useWideViewPort = true
        }

        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                super.onReceivedError(view, request, error)
                // 只对主框架导航失败弹提示，子资源（favicon/图片）失败静默
                if (request?.isForMainFrame == true) {
                    Toast.makeText(
                        this@MainActivity,
                        "页面加载失败，请重启 App",
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }

            // 拦截外部链接：非 127.0.0.1 的 URL 交给系统浏览器，防止模型输出的链接顶替聊天 UI
            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?
            ): Boolean {
                val url = request?.url ?: return false
                if (url.host == "127.0.0.1" || url.host == "localhost") return false
                try {
                    startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, url))
                } catch (_: Exception) { }
                return true
            }
        }

        // 处理麦克风权限请求（Web Speech API）—— 仅在系统权限已授予时才放行
        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: android.webkit.PermissionRequest?) {
                val granted = ContextCompat.checkSelfPermission(
                    this@MainActivity, Manifest.permission.RECORD_AUDIO
                ) == PackageManager.PERMISSION_GRANTED
                if (granted && request != null) {
                    request.grant(arrayOf(android.webkit.PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                } else {
                    request?.deny()
                }
            }

            // 接管 <input type="file">：相册 / 视频 / 头像上传都走这里
            override fun onShowFileChooser(
                webView: WebView?,
                newCallbackPath: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                // 释放上一次未完成的回调，避免连续点击时 WebView 卡死
                filePathCallback?.onReceiveValue(null)
                filePathCallback = newCallbackPath

                val accepts = fileChooserParams?.acceptTypes?.filter { it.isNotBlank() } ?: emptyList()
                val mime = when {
                    accepts.isEmpty() -> "*/*"
                    accepts.size == 1 && accepts[0].contains("/") -> accepts[0]
                    accepts.all { it.startsWith("image/") } -> "image/*"
                    accepts.all { it.startsWith("video/") } -> "video/*"
                    else -> "*/*"
                }

                // <input capture="environment"> → 直接调系统相机拍一张
                if (fileChooserParams?.isCaptureEnabled == true) {
                    val cam = buildCameraIntent()
                    if (cam != null) {
                        try {
                            cameraLauncher.launch(cam)
                            return true
                        } catch (e: Exception) {
                            cameraOutputUri = null // 没有可用相机，回退到文件选择器
                        }
                    }
                }

                // 自己拼 ACTION_GET_CONTENT：createIntent() 对 capture="environment"
                // 会生成需要输出 URI 的拍照 Intent，部分机型直接崩或返回空。
                val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                    type = mime
                    addCategory(Intent.CATEGORY_OPENABLE)
                    if (accepts.size > 1) {
                        putExtra(Intent.EXTRA_MIME_TYPES, accepts.toTypedArray())
                    }
                    if (fileChooserParams?.mode == FileChooserParams.MODE_OPEN_MULTIPLE) {
                        putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                    }
                }

                return try {
                    fileChooserLauncher.launch(intent)
                    true
                } catch (e: Exception) {
                    filePathCallback = null
                    newCallbackPath?.onReceiveValue(null)
                    Toast.makeText(
                        this@MainActivity, "打不开文件选择器", Toast.LENGTH_SHORT
                    ).show()
                    false
                }
            }
        }

        webView.loadUrl(url)
    }

    // ----------------------------------------------------------------------
    // 手势返回：WebView 有历史就 goBack，否则退出
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (::webView.isInitialized && webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
