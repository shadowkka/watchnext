package com.example

import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.*
import android.widget.*
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import com.example.data.legacyWebAddress
import kotlinx.coroutines.launch
import org.json.JSONObject

/** The server owns the UI and settings. APK updates never clear WebView or DataStore data. */
class MainActivity : ComponentActivity() {
    private val prefs by lazy { getSharedPreferences("watchnext_web", MODE_PRIVATE) }
    private lateinit var web: WebView
    private lateinit var root: LinearLayout
    private lateinit var progress: ProgressBar
    private lateinit var error: TextView
    private var base = ""
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var exportBytes: ByteArray? = null
    private var pendingMagnet: String? = null
    private val chooseFile = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        fileCallback?.onReceiveValue(uri?.let { arrayOf(it) }); fileCallback = null
    }
    private val saveFile = registerForActivityResult(ActivityResultContracts.CreateDocument("application/json")) { uri ->
        val bytes = exportBytes; exportBytes = null
        if (uri != null && bytes != null) runCatching { contentResolver.openOutputStream(uri)?.use { it.write(bytes) } }
            .onFailure { toast("Не удалось сохранить файл") }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(SystemBarStyle.dark(Color.TRANSPARENT), SystemBarStyle.dark(Color.TRANSPARENT))
        pendingMagnet = intent?.dataString?.takeIf { it.startsWith("magnet:?", true) }
        root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(Color.rgb(16,19,26)) }
        setContentView(root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val safe = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            view.setPadding(safe.left, safe.top, safe.right, safe.bottom); insets
        }
        val toolbar = LinearLayout(this)
        toolbar.addView(TextView(this).apply { text = "WatchNext"; textSize = 18f; setTextColor(Color.WHITE); setPadding(20,12,12,12) }, LinearLayout.LayoutParams(0, -2, 1f))
        toolbar.addView(Button(this).apply { text = "↻"; contentDescription = "Обновить"; setOnClickListener { web.reload() } })
        toolbar.addView(Button(this).apply { text = "Сервер"; setOnClickListener { connectionDialog() } })
        root.addView(toolbar)
        progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal)
        root.addView(progress, LinearLayout.LayoutParams(-1, 4))
        error = TextView(this).apply { setTextColor(Color.WHITE); setPadding(24,20,24,20); visibility = View.GONE }
        root.addView(error)
        web = WebView(this).apply {
            setBackgroundColor(Color.rgb(16,19,26))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = true // Only user-selected SAF documents are exposed to file inputs.
            settings.setSupportMultipleWindows(true)
            settings.javaScriptCanOpenWindowsAutomatically = false
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            settings.userAgentString += " WatchNextAndroid/2"
            CookieManager.getInstance().setAcceptThirdPartyCookies(this, false)
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    if (!request.isForMainFrame) return false
                    return navigate(request.url)
                }
                override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
                    error.visibility = View.GONE
                }
                override fun onPageFinished(view: WebView, url: String) {
                    val magnet = pendingMagnet ?: return
                    if (!WebAddress.sameOrigin(base, url)) return
                    pendingMagnet = null
                    // A DOM event opens the normal confirmation form; it never adds automatically.
                    web.evaluateJavascript("window.dispatchEvent(new CustomEvent('watchnext-magnet',{detail:${JSONObject.quote(magnet)}}));", null)
                }
                override fun onReceivedError(view: WebView, request: WebResourceRequest, failure: WebResourceError) {
                    if (request.isForMainFrame) showError("Сервер недоступен. Проверьте Wi-Fi или VPN и адрес через кнопку «Сервер», затем нажмите ↻.")
                }
                override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                    if (request.isForMainFrame && response.statusCode >= 400) showError("Ошибка сервера ${response.statusCode}. Повторите загрузку или проверьте адрес.")
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onProgressChanged(view: WebView, value: Int) { this@MainActivity.progress.progress = value; this@MainActivity.progress.visibility = if (value == 100) View.GONE else View.VISIBLE }
                override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                    if (!WebAddress.sameOrigin(base, view.url ?: "")) return false
                    fileCallback?.onReceiveValue(null); fileCallback = callback
                    chooseFile.launch(arrayOf("application/json", "text/*", "application/vnd.ms-excel", "application/octet-stream")); return true
                }
                override fun onCreateWindow(view: WebView, dialog: Boolean, userGesture: Boolean, message: android.os.Message): Boolean {
                    if (!userGesture) return false
                    val popup = WebView(this@MainActivity)
                    popup.webViewClient = object : WebViewClient() {
                        override fun shouldOverrideUrlLoading(v: WebView, request: WebResourceRequest): Boolean {
                            openExternal(request.url); popup.destroy(); return true
                        }
                    }
                    (message.obj as WebView.WebViewTransport).webView = popup; message.sendToTarget(); return true
                }
            }
            setDownloadListener { url, _, _, _, _ ->
                if (url.startsWith("data:application/json;charset=utf-8,")) {
                    val bytes = Uri.decode(url.substringAfter(',')).toByteArray(Charsets.UTF_8)
                    if (bytes.size <= 6 * 1024 * 1024) { exportBytes = bytes; saveFile.launch("watchnext-watched.json") }
                    else toast("Список слишком большой")
                } else if (url.startsWith("http://") || url.startsWith("https://")) openExternal(Uri.parse(url))
                else toast("Не удалось скачать файл")
            }
        }
        root.addView(web, LinearLayout.LayoutParams(-1, 0, 1f))
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                web.evaluateJavascript("(()=>{const s=document.querySelector('#sheet');if(s?.open){s.querySelector('[data-action=close]')?.click();return true}return false})()") { closed ->
                    if (closed != "true") { if (web.canGoBack()) web.goBack() else finish() }
                }
            }
        })
        lifecycleScope.launch {
            val saved = prefs.getString("url", null)
            base = saved ?: legacyWebAddress(applicationContext)
            if (saved != null) {
                val history = savedInstanceState?.getBundle("web_state")
                if (history == null || web.restoreState(history) == null) loadServer()
            } else connectionDialog()
        }
    }
    private fun loadServer() {
        prefs.edit().putString("url", base).apply()
        web.clearHistory() // Navigation only; cookies/storage and native preferences remain intact.
        web.loadUrl(base)
    }
    private fun connectionDialog() {
        val field = EditText(this).apply { setSingleLine(); inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI; setText(base.ifBlank { "http://watchnext.local/" }) }
        val dialog = AlertDialog.Builder(this).setTitle("Веб-интерфейс WatchNext")
            .setMessage("Адрес веба на вашем сервере, например http://watchnext.local или http://IP:8787. Старый API на :8000 здесь не подходит.")
            .setView(field).setNegativeButton("Отмена", null).setPositiveButton("Подключиться", null).create()
        dialog.setOnShowListener { dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            runCatching { WebAddress.normalize(field.text.toString()) }.onSuccess { next ->
                base = next; dialog.dismiss(); loadServer()
            }.onFailure { field.error = it.message }
        } }; dialog.show()
    }
    private fun navigate(uri: Uri): Boolean {
        if (uri.toString().startsWith("data:application/json;charset=utf-8,")) return false
        if (WebAddress.sameOrigin(base, uri.toString())) return false
        openExternal(uri); return true
    }
    private fun openExternal(uri: Uri) {
        if (uri.scheme !in listOf("https", "http", "magnet", "jellyfin")) return
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)) }.onFailure { toast("Не найдено приложение для ссылки") }
    }
    private fun showError(message: String) { error.text = message; error.visibility = View.VISIBLE; progress.visibility = View.GONE }
    private fun toast(message: String) { Toast.makeText(this, message, Toast.LENGTH_LONG).show() }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent); setIntent(intent)
        val magnet = intent.dataString?.takeIf { it.startsWith("magnet:?", true) } ?: return
        pendingMagnet = magnet
        if (base.isNotBlank()) web.reload()
    }
    override fun onSaveInstanceState(outState: Bundle) {
        val history = Bundle(); web.saveState(history); outState.putBundle("web_state", history)
        super.onSaveInstanceState(outState)
    }
    override fun onPause() { web.onPause(); super.onPause() }
    override fun onResume() { super.onResume(); if (::web.isInitialized) web.onResume() }
    override fun onDestroy() { fileCallback?.onReceiveValue(null); web.destroy(); super.onDestroy() }
}
