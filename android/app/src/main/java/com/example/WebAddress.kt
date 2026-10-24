package com.example

import java.net.URI

/** Separate web address from the old REST-only endpoint without touching server settings. */
object WebAddress {
    fun normalize(input: String, migrateApi: Boolean = false): String {
        val raw = input.trim().let { if (it.contains("://")) it else "http://$it" }
        val uri = URI(raw)
        require(uri.scheme in listOf("http", "https") && !uri.host.isNullOrBlank() && uri.rawUserInfo == null) {
            "Введите HTTP(S) адрес веб-интерфейса WatchNext"
        }
        require(uri.rawQuery == null && uri.rawFragment == null) { "Введите адрес без параметров и #" }
        val port = if (migrateApi && uri.port == 8000) 8787 else uri.port
        return URI(uri.scheme, null, uri.host, port, uri.path.ifEmpty { "/" }, null, null).toASCIIString().trimEnd('/') + "/"
    }
    fun sameOrigin(base: String, target: String): Boolean = runCatching {
        val a = URI(base); val b = URI(target)
        fun port(u: URI) = if (u.port != -1) u.port else if (u.scheme == "https") 443 else 80
        a.scheme == b.scheme && a.host.equals(b.host, true) && port(a) == port(b) && b.rawUserInfo == null
    }.getOrDefault(false)
}
