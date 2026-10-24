package com.example

import android.content.Context
import com.example.data.legacySettings
import com.example.data.legacyWebAddress
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.core.intPreferencesKey
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.flow.first
import android.view.ViewGroup
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class WebShellTest {
    @Test fun oldApiAddressMigrationPreservesHostAndCustomWebPorts() {
        assertEquals("http://mini.local:8787/", WebAddress.normalize("http://mini.local:8000", true))
        assertEquals("http://10.4.5.6:8787/", WebAddress.normalize("http://10.4.5.6:8000", true))
        assertEquals("https://example.test/watchnext/", WebAddress.normalize("https://example.test/watchnext/", true))
        assertEquals("http://mini.local:9999/", WebAddress.normalize("mini.local:9999", true))
        assertFalse(WebAddress.sameOrigin("http://mini.local/", "http://mini.local:8096/"))
        assertFalse(WebAddress.sameOrigin("http://mini.local/", "http://mini.local.evil.test/"))
        assertTrue(WebAddress.sameOrigin("http://mini.local/", "http://mini.local:80/#search"))
        for (bad in listOf("javascript:alert(1)", "file:///data/private", "https://user:password@example.test", "http://mini.local/?token=abc")) {
            assertTrue(bad, runCatching { WebAddress.normalize(bad) }.isFailure)
        }
    }
    @Test fun legacyConnectionIsReadWithoutOverwritingSettings() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.legacySettings.edit {
            it[stringPreferencesKey("server_url")] = "http://old-mini.local:8000"
            it[intPreferencesKey("episode_limit")] = 27
        }
        assertEquals("http://old-mini.local:8787/", legacyWebAddress(context))
        assertEquals(27, context.legacySettings.data.first()[intPreferencesKey("episode_limit")])
    }
    @Test fun savedServerAndBrowserSettingsSurviveActivityRecreation() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val prefs = context.getSharedPreferences("watchnext_web", Context.MODE_PRIVATE)
        prefs.edit().clear().putString("url", "http://mini.local:8787/").commit()
        fun findWeb(group: ViewGroup): WebView? {
            for (i in 0 until group.childCount) {
                val child = group.getChildAt(i)
                if (child is WebView) return child
                if (child is ViewGroup) findWeb(child)?.let { return it }
            }
            return null
        }
        repeat(2) {
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val web = findWeb(controller.get().findViewById(android.R.id.content))!!
            assertEquals("http://mini.local:8787/", shadowOf(web).lastLoadedUrl)
            assertTrue(web.settings.domStorageEnabled)
            assertTrue(web.settings.javaScriptEnabled)
            assertFalse(web.settings.allowFileAccess)
            assertTrue(web.settings.userAgentString.contains("WatchNextAndroid/"))
            controller.pause().stop().destroy()
            assertEquals("http://mini.local:8787/", prefs.getString("url", ""))
        }
    }
}
