package com.example.data
import android.content.Context
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import com.example.WebAddress
// Read the previous address once. Never write native queue limits to the server.
internal val Context.legacySettings by preferencesDataStore(name = "watchnext_settings")
suspend fun legacyWebAddress(context: Context): String = runCatching {
    val url = context.legacySettings.data.first()[stringPreferencesKey("server_url")]
    if (url.isNullOrBlank()) "http://watchnext.local/" else WebAddress.normalize(url, migrateApi = true)
}.getOrDefault("http://watchnext.local/")
