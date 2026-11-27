import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { t } from '../lib/i18n'
import { PLATFORMS, WOOPSOCIAL_PLATFORMS, platformLabel } from '../lib/uploadpost'
import { woopTargetLabel, type WoopAccount } from '../lib/woopTargets'
import type { WoopProfile } from '../lib/uploadpost'

/** Settings for publishing through WoopSocial.
 *
 *  The provider Clips Kitty leads with, and the default in the editor.
 *  Bring-your-own-key like the other one: the creator's own account, their
 *  own connected socials, their own allowance. Clips Kitty holds no shared
 *  key and pays for nothing.
 *
 *  Why this one first: their free plan connects two accounts with API
 *  access, which covers most creators outright. Not unlimited, whatever their
 *  pricing page says: YouTube is about five posts a day (see the warning
 *  below the setup steps).
 *  Upload-Post reaches more destinations and does thumbnails, first comments
 *  and a posting queue, so it stays available below as the alternative.
 *
 *  Two rules this card must never break: display the key back, or claim an
 *  affiliate relationship that does not exist. The key is write-only over
 *  the API, and the referral CTA appears only once a URL is configured.
 */

const SIGNUP = 'https://woopsocial.com/'
const DASHBOARD = 'https://app.woopsocial.com/api-access'

type Status = {
  enabled: boolean
  has_key: boolean
  key_tail: string
  platforms: string[]
  common_description: string
  affiliate_url: string
  storage: string
  project_id: string
  /** Named API-key profiles, never the key material. */
  accounts?: WoopProfile[]
  /** Which named key the Settings card manages. */
  selected_account_id?: string
}

const empty: Status = {
  enabled: false,
  has_key: false,
  key_tail: '',
  platforms: [],
  common_description: '',
  affiliate_url: '',
  storage: '',
  project_id: '',
  accounts: [],
  selected_account_id: 'default'
}

export default function WoopSocialCard(): JSX.Element {
  const [status, setStatus] = useState<Status>(empty)
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [connected, setConnected] = useState<string[]>([])
  const [connectedAccounts, setConnectedAccounts] = useState<WoopAccount[]>([])
  // Named API-key profiles (profiles(status.accounts)), not connected socials.
  const [profiles, setProfiles] = useState<WoopProfile[]>([])
  const [selectedProfileId, setSelectedProfileId] = useState('default')
  const [offered, setOffered] = useState('')
  const [affiliateDraft, setAffiliateDraft] = useState('')
  // Adding / naming a profile.
  const [addName, setAddName] = useState('')
  const [addKey, setAddKey] = useState('')
  const [adding, setAdding] = useState(false)
  // Renaming a profile.
  const [renameId, setRenameId] = useState<string | null>(null)
  const [renameName, setRenameName] = useState('')
  const mounted = useRef(true)

  const load = (): void => {
    api
      .woopSocialStatus()
      .then((s) => {
        if (!mounted.current) return
        setStatus(s as unknown as Status)
        setAffiliateDraft(s.affiliate_url)
        setProfiles(s.accounts ?? [])
        setSelectedProfileId(s.selected_account_id ?? 'default')
        // Only when it's on and has a key: asked otherwise, the engine
        // answers "not enabled", an error in the log each time Settings opens.
        if (s.enabled && s.has_key)
          api
            .woopSocialConnections()
            .then((got) => {
              if (!mounted.current) return
              setConnected(got.connected)
              setConnectedAccounts(got.accounts ?? [])
            })
            .catch(() => {
              /* the linked accounts show once WoopSocial answers */
            })
      })
      .catch(() => {
        /* backend not up yet */
      })
  }

  useEffect(() => {
    mounted.current = true
    load()
    return () => {
      mounted.current = false
    }
  }, [])

  const say = (m: string): void => {
    setError('')
    setNotice(m)
  }
  const failed = (e: unknown): void => {
    setNotice('')
    setError(String(e).replace(/^Error:\s*/, ''))
  }

  const toggle = async (enabled: boolean): Promise<void> => {
    try {
      setStatus((await api.patchWoopSocialSettings({ enabled })) as unknown as Status)
    } catch (e) {
      failed(e)
    }
  }

  /** Save the key for the currently selected profile. The legacy / key
   *  routes keep managing whichever profile Settings has selected, so an
   *  install upgraded from a single key needs no reconnect. */
  const saveKey = async (value?: string): Promise<void> => {
    const key = (value ?? apiKey).trim()
    if (!key || busy) return
    setBusy(true)
    setOffered('')
    try {
      const got = await api.putWoopSocialKey(key)
      setStatus(got as unknown as Status)
      setApiKey('')
      setShowKey(false)
      setProfiles(got.accounts ?? [])
      setSelectedProfileId(got.selected_account_id ?? 'default')
      say(t('Connected.'))
      load()
    } catch (e) {
      failed(e)
    } finally {
      setBusy(false)
    }
  }

  const removeKey = async (): Promise<void> => {
    if (busy) return
    setBusy(true)
    try {
      setStatus((await api.deleteWoopSocialKey()) as unknown as Status)
      setConnected([])
      setProfiles([])
      say(t('API key removed.'))
    } catch (e) {
      failed(e)
    } finally {
      setBusy(false)
    }
  }

  /** Add a new named profile, optionally with its key right away. */
  const addProfile = async (): Promise<void> => {
    const name = addName.trim()
    const key = addKey.trim()
    if (!name || adding || busy) return
    setAdding(true)
    try {
      const got = await api.woopSocialAddAccount({ name, api_key: key || undefined })
      // woopSocialAddAccount returns { account: WoopProfile; projects: number }
      const profile: WoopProfile = got.account
      setStatus({
        enabled: status.enabled,
        has_key: status.has_key,
        key_tail: status.key_tail,
        platforms: status.platforms,
        common_description: status.common_description,
        affiliate_url: status.affiliate_url,
        storage: status.storage,
        project_id: status.project_id,
        accounts: [...(status.accounts ?? []), profile],
        selected_account_id: profile.id
      } as unknown as Status)
      setProfiles(status.accounts ? [...status.accounts, profile] : [profile])
      setSelectedProfileId(profile.id)
      setAddName('')
      setAddKey('')
      say(t('Profile added.'))
    } catch (e) {
      failed(e)
    } finally {
      setAdding(false)
    }
  }

  const saveKeyForProfile = async (accountId: string, key: string): Promise<void> => {
    if (!key || busy) return
    setBusy(true)
    try {
      const got = await api.woopSocialPutNamedKey(accountId, key)
      setProfiles(got.accounts ?? [])
      say(t('Key saved.'))
    } catch (e) {
      failed(e)
    } finally {
      setBusy(false)
    }
  }

  const removeProfile = async (accountId: string): Promise<void> => {
    if (busy) return
    setBusy(true)
    try {
      const got = await api.woopSocialRemoveAccount(accountId)
      setStatus(got as unknown as Status)
      setProfiles(got.accounts ?? [])
      setSelectedProfileId(got.selected_account_id ?? 'default')
      say(t('Profile removed.'))
    } catch (e) {
      failed(e)
    } finally {
      setBusy(false)
    }
  }

  const selectProfile = async (accountId: string): Promise<void> => {
    if (busy) return
    setBusy(true)
    try {
      const got = await api.woopSocialSelectAccount(accountId)
      setStatus(got as unknown as Status)
      setProfiles(got.accounts ?? [])
      setSelectedProfileId(got.selected_account_id ?? 'default')
    } catch (e) {
      failed(e)
    } finally {
      setBusy(false)
    }
  }

  const startRename = (profile: WoopProfile): void => {
    setRenameId(profile.id)
    setRenameName(profile.name)
  }

  const saveRename = async (): Promise<void> => {
    if (!renameId || busy) return
    setBusy(true)
    try {
      const got = await api.woopSocialRenameAccount(renameId, renameName.trim())
      setProfiles((current) =>
        current.map((p) => (p.id === renameId ? { ...p, name: got.name } : p))
      )
      setRenameId(null)
      setRenameName('')
    } catch (e) {
      failed(e)
    } finally {
      setBusy(false)
    }
  }

  const cancelRename = (): void => {
    setRenameId(null)
    setRenameName('')
  }

  /** WoopSocial authorises one platform at a time rather than offering one
   *  hosted page for all of them, so each gets its own button. */
  const connect = async (platform: string): Promise<void> => {
    if (busy) return
    setBusy(true)
    try {
      const got = await api.woopSocialConnect(platform)
      const opened = await window.studio.openExternal(got.url)
      if (opened) {
        say(
          `${t('Opened')} ${platformLabel(platform)} ${t('in your browser. Come back when it is linked.')}`
        )
        window.addEventListener(
          'focus',
          () => {
            api
              .woopSocialConnections()
              .then((c) => {
                if (!mounted.current) return
                setConnected(c.connected)
                setConnectedAccounts(c.accounts ?? [])
              })
              .catch(() => {})
          },
          { once: true }
        )
      } else {
        failed(t('Could not open your browser.'))
      }
    } catch (e) {
      failed(e)
    } finally {
      setBusy(false)
    }
  }

  const saveAffiliate = async (): Promise<void> => {
    const url = affiliateDraft.trim()
    if (url === status.affiliate_url) return
    if (url && !/^https:\/\//i.test(url)) {
      failed(t('That needs to be a full https:// link.'))
      return
    }
    try {
      setStatus(
        (await api.patchWoopSocialSettings({ affiliate_url: url })) as unknown as Status
      )
      say(url ? t('Referral link saved.') : t('Referral link cleared.'))
    } catch (e) {
      failed(e)
    }
  }

  const watchForKey = (): void => {
    window.addEventListener(
      'focus',
      () => {
        window.studio
          .readClipboardKey()
          .then((found) => mounted.current && found && setOffered(found))
          .catch(() => {})
      },
      { once: true }
    )
  }

  const open = (url: string) => () => {
    void window.studio.openExternal(url)
    if (!status.has_key) watchForKey()
  }

  const affiliate = status.affiliate_url.trim()
  const mine = PLATFORMS.filter((p) => WOOPSOCIAL_PLATFORMS.includes(p.id))
  /** The legacy single key, shown as the "default" profile so old installs
   *  keep working with no reconnect. */
  const defaultProfile: WoopProfile = {
    id: 'default',
    name: t('My WoopSocial'),
    project_id: status.project_id ?? '',
    has_key: status.has_key,
    key_tail: status.key_tail,
    platforms: status.platforms
  }

  const isDefault = (id: string) => id === 'default'
  // Named profiles only; the original ("default") key is managed by the
  // Default key section below so old installs keep working with no reconnect.
  const profileList = profiles.filter((p) => p.id !== 'default')
  const selected = profileList.find((p) => p.id === selectedProfileId) ?? defaultProfile

  return (
    <section className="card space-y-3" aria-label="WoopSocial publishing">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="font-semibold">{t('Publish through WoopSocial')}</h3>
          <p className="text-xs text-muted mt-1 max-w-xl">
            {t(
              'Send a clip to YouTube, TikTok, Instagram and more in one go, through your own WoopSocial account. Their free plan connects two accounts. Your accounts are connected on WoopSocial, not here, so Clips Kitty never asks for a social password.'
            )}
          </p>
        </div>
        <label className="inline-flex items-center gap-2 text-sm shrink-0">
          <input
            type="checkbox"
            checked={status.enabled}
            disabled={busy}
            onChange={(e) => void toggle(e.target.checked)}
            aria-label={t('Enable publishing through WoopSocial')}
          />
          {status.enabled ? t('On') : t('Off')}
        </label>
      </div>

      {status.enabled && (
        <>
          <p className="text-[11px] text-muted border-l-2 border-raised pl-2">
            {t(
              'Publishing this way sends the clip and its details to WoopSocial, which delivers them to the platforms you pick. Everything else in Clips Kitty still runs on your PC.'
            )}
          </p>

          {/* Named API keys. Each is a full WoopSocial account with its own
              encrypted key, so one clip can go to the same platform through
              two different keys without one overwriting the other. */}
          <div className="space-y-2">
            <label className="label block">{t('API keys')}</label>
            <div className="space-y-1">
              {profileList.map((profile) => (
                <ProfileRow
                  key={profile.id}
                  profile={profile}
                  selected={selectedProfileId === profile.id}
                  isDefault={isDefault(profile.id)}
                  onSelect={selectProfile}
                  onSaveKey={(id, key) => saveKeyForProfile(id, key)}
                  onRemove={removeProfile}
                  onStartRename={startRename}
                  onSaveRename={saveRename}
                  onCancelRename={cancelRename}
                  renaming={renameId === profile.id}
                  renameName={renameName}
                  onChangeRenameName={setRenameName}
                  busy={busy || adding}
                />
              ))}
            </div>
            <div className="flex gap-2 flex-wrap">
              <input
                id="woopsocial-profile-name"
                className="input !py-1 text-sm min-w-52"
                type="text"
                value={addName}
                placeholder={t('Name this key')}
                onChange={(e) => setAddName(e.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
              <input
                id="woopsocial-profile-key"
                className="input !py-1 text-sm min-w-52"
                type="password"
                value={addKey}
                placeholder={t('Paste its WoopSocial API key (optional)')}
                onChange={(e) => setAddKey(e.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
              <button
                className="btn-accent !py-1 !px-3 text-xs"
                disabled={busy || adding || !addName.trim()}
                onClick={() => void addProfile()}
              >
                {adding ? t('Adding…') : t('Add key')}
              </button>
            </div>
          </div>

          <div className="space-y-2">
            <label className="label block" htmlFor="woopsocial-key">
              {t('Default key (Settings)')}
            </label>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm text-success">
                ● {t('Key saved')}
                {status.key_tail && <span className="text-muted"> ····{status.key_tail}</span>}
                <span className="text-muted">
                  {' · '}
                  {t('Default: ')}
                  {selected.name}
                </span>
              </span>
              <button className="btn-ghost !py-1 !px-3 text-xs" onClick={() => void removeKey()}>
                {t('Remove')}
              </button>
              {status.storage && (
                <span className="text-[11px] text-muted">
                  {t('Stored with')} {status.storage}
                </span>
              )}
            </div>
            {!status.has_key && (
              <>
                {offered && (
                  <div className="flex items-center gap-2 flex-wrap border border-accent/40 bg-accent/10 rounded-lg p-2 mb-2">
                    <span className="text-xs">
                      {t('Found a key on your clipboard')}{' '}
                      <span className="text-muted">····{offered.slice(-4)}</span>
                    </span>
                    <button
                      className="btn-accent !py-1 !px-3 text-xs"
                      disabled={busy}
                      onClick={() => void saveKey(offered)}
                    >
                      {t('Use it')}
                    </button>
                    <button
                      className="btn-ghost !py-1 !px-3 text-xs"
                      onClick={() => setOffered('')}
                    >
                      {t('No thanks')}
                    </button>
                  </div>
                )}
                <div className="flex gap-2 flex-wrap">
                  <input
                    id="woopsocial-key"
                    className="input flex-1 !py-1 text-sm min-w-52"
                    type={showKey ? 'text' : 'password'}
                    value={apiKey}
                    placeholder={t('Paste your WoopSocial API key')}
                    onChange={(e) => setApiKey(e.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <button
                    className="btn-ghost !py-1 !px-3 text-xs"
                    onClick={() => setShowKey((v) => !v)}
                  >
                    {showKey ? t('Hide') : t('Show')}
                  </button>
                  <button
                    className="btn-accent !py-1 !px-3 text-xs"
                    disabled={busy || !apiKey.trim()}
                    onClick={() => void saveKey()}
                  >
                    {busy ? t('Checking…') : t('Save and check')}
                  </button>
                </div>
              </>
            )}
          </div>

          {status.has_key && (
            <div className="space-y-2">
              <p className="label">{t('Connected accounts')}</p>
              <div className="flex flex-wrap gap-2">
                {mine.map((p) => {
                  const on = connected.includes(p.id)
                  return (
                    <button
                      key={p.id}
                      className={`px-2.5 py-1 rounded-lg text-xs border ${
                        on
                          ? 'border-success/50 text-success'
                          : 'border-raised text-muted hover:text-ink'
                      }`}
                      disabled={busy}
                      onClick={() => void connect(p.id)}
                      title={on ? t('Connected - click to reconnect') : t('Click to connect')}
                    >
                      {on ? '● ' : '○ '}
                      {on ? woopTargetLabel(p.id, connectedAccounts) : p.label}
                    </button>
                  )
                })}
              </div>
              <p className="text-[11px] text-muted">
                {t(
                  'Each opens WoopSocial in your browser. Their free plan allows two connected accounts.'
                )}
              </p>
            </div>
          )}

          {!status.has_key && (
            <div className="border border-raised rounded-lg p-3 space-y-2">
              {/* Numbered, because "copy your API key from their dashboard"
                  is one sentence that assumes the reader has met an API key
                  before. Most creators have not, and that is the step they
                  stop on. Each step keeps its button beside it. */}
              <p className="text-sm font-medium">{t('Set up posting in 3 steps')}</p>
              <ol className="space-y-2">
                <li className="flex items-start gap-2">
                  <span className="text-xs text-muted shrink-0 mt-1">1.</span>
                  <div className="min-w-0">
                    <p className="text-xs text-ink">{t('Make a free WoopSocial account.')}</p>
                    <button
                      className="btn-accent !py-1 !px-3 text-xs mt-1"
                      onClick={open(affiliate || SIGNUP)}
                    >
                      {t('Create a free account ↗')}
                    </button>
                  </div>
                </li>
                <li className="flex items-start gap-2">
                  <span className="text-xs text-muted shrink-0 mt-1">2.</span>
                  <div className="min-w-0">
                    <p className="text-xs text-ink">
                      {t('On their site open API access, then create a key and copy it.')}
                    </p>
                    <button
                      className="btn-ghost !py-1 !px-3 text-xs mt-1"
                      onClick={open(DASHBOARD)}
                    >
                      {t('Open API access ↗')}
                    </button>
                  </div>
                </li>
                <li className="flex items-start gap-2">
                  <span className="text-xs text-muted shrink-0 mt-1">3.</span>
                  <p className="text-xs text-ink">
                    {t('Paste the key in the box above and press Save and check.')}
                  </p>
                </li>
              </ol>
              {/* Their own pricing page says "unlimited posts". YouTube is
                  not: they ration one Google Cloud project between all their
                  users. Saying nothing here is how 37 clips went out at once
                  and 32 were rejected. */}
              <p className="text-xs text-warn">
                {t(
                  'Worth knowing: YouTube allows about 5 posts a day on the free plan. Use the posts-a-day setting when you publish and Clips Kitty spreads them out for you.'
                )}
              </p>
              {affiliate && (
                <p className="text-[11px] text-muted">
                  {t(
                    'Affiliate link - Clips Kitty may earn a commission if you sign up through it, at no extra cost to you.'
                  )}
                </p>
              )}
            </div>
          )}

          {status.has_key && (
            <details className="text-[11px] text-muted">
              <summary className="cursor-pointer">{t('Affiliate referral link')}</summary>
              <input
                className="input !py-1 text-xs w-full mt-1.5"
                value={affiliateDraft}
                placeholder="https://…"
                onChange={(e) => setAffiliateDraft(e.target.value)}
                onBlur={() => void saveAffiliate()}
                aria-label="WoopSocial affiliate referral URL"
              />
            </details>
          )}

          <div className="text-[11px] text-muted">
            {t('Can publish to:')} {mine.map((p) => p.label).join(', ')}.{' '}
            {t('Bluesky needs Upload-Post instead.')}
          </div>
        </>
      )}

      {notice && <p className="text-xs text-success">{notice}</p>}
      {error && <p className="text-xs text-error">{error}</p>}
    </section>
  )
}

/** One named key row: its name, masked tail, whether it holds a key, and the
 *  controls to select, re-key, rename or remove it. The default ("legacy")
   *  key can't be removed — it has the un-suffixed secret old installs rely on. */
function ProfileRow({
  profile,
  selected,
  isDefault,
  onSelect,
  onSaveKey,
  onRemove,
  onStartRename,
  onSaveRename,
  onCancelRename,
  renaming,
  renameName,
  onChangeRenameName,
  busy
}: {
  profile: WoopProfile
  selected: boolean
  isDefault: boolean
  onSelect: (id: string) => void
  onSaveKey: (accountId: string, key: string) => Promise<void>
  onRemove: (id: string) => void
  onStartRename: (p: WoopProfile) => void
  onSaveRename: () => void
  onCancelRename: () => void
  renaming: boolean
  renameName: string
  onChangeRenameName: (v: string) => void
  busy: boolean
}): JSX.Element {
  const [key, setKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [saving, setSaving] = useState(false)
  return (
    <div className="flex items-center gap-2 flex-wrap border border-raised rounded-lg p-2">
      <div className="flex-1 min-w-0 flex flex-col sm:flex-row sm:items-center sm:gap-2">
        {renaming ? (
          <input
            className="input !py-1 text-sm"
            value={renameName}
            onChange={(e) => onChangeRenameName(e.target.value)}
            onBlur={onSaveRename}
            onKeyDown={(e) => e.key === 'Enter' && onSaveRename()}
            autoFocus
          />
        ) : (
          <span className="text-sm font-medium truncate" title={profile.name}>
            {profile.name}
          </span>
        )}
        {profile.has_key && profile.key_tail && (
          <span className="text-[11px] text-muted">····{profile.key_tail}</span>
        )}
        {!profile.has_key && (
          <span className="text-[11px] text-warn">{t('no key yet')}</span>
        )}
        {selected && <span className="text-[10px] text-accent font-medium">· {t('default')}</span>}
        {isDefault && (
          <span className="text-[10px] text-muted">{t('original key')}</span>
        )}
      </div>
      <div className="flex gap-1 shrink-0">
        {!isDefault && (
          <button
            className="btn-ghost !py-1 !px-2 text-xs"
            disabled={busy || renaming}
            onClick={() => onSelect(profile.id)}
            aria-pressed={selected}
            title={selected ? t('Already the default') : t('Make this the default key')}
          >
            {selected ? t('Default') : t('Use')}
          </button>
        )}
        {!renaming && (
          <>
            <button
              className="btn-ghost !py-1 !px-2 text-xs"
              disabled={busy}
              onClick={() => onStartRename(profile)}
              title={t('Rename')}
            >
              {t('Name')}
            </button>
            <button
              className="btn-ghost !py-1 !px-2 text-xs"
              disabled={busy || isDefault}
              onClick={() => void onRemove(profile.id)}
              title={t('Remove this key')}
              aria-disabled={isDefault}
            >
              {t('Remove')}
            </button>
          </>
        )}
        {renaming && (
          <>
            <button
              className="btn-ghost !py-1 !px-2 text-xs"
              disabled={busy}
              onClick={onSaveRename}
              title={t('Save')}
            >
              {t('Save')}
            </button>
            <button
              className="btn-ghost !py-1 !px-2 text-xs"
              onClick={onCancelRename}
              title={t('Cancel')}
            >
              {t('Cancel')}
            </button>
          </>
        )}
        {!renaming && !profile.has_key && (
          <div className="flex gap-1">
            <input
              id={`woopsocial-key-${profile.id}`}
              className="input !py-1 text-xs min-w-48"
              type={showKey ? 'text' : 'password'}
              value={key}
              placeholder={t('Paste its API key')}
              onChange={(e) => setKey(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
            <button
              className="btn-accent !py-1 !px-2 text-xs"
              disabled={busy || saving || !key.trim()}
              onClick={() => {
                setSaving(true)
                onSaveKey(profile.id, key).then(() => setSaving(false), () => setSaving(false))
              }}
              title={t('Save this key for the profile')}
            >
              {saving ? t('Saving…') : t('Key')}
            </button>
            <button
              className="btn-ghost !py-1 !px-2 text-xs"
              onClick={() => setShowKey((v) => !v)}
            >
              {showKey ? t('Hide') : t('Show')}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
