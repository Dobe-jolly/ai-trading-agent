import * as React from 'react'
import { Mail, UserRound } from 'lucide-react'

import { Confidence } from '@/components/confidence'
import { StepEyebrow } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'
import type { DecisionDetail, DeskVocab } from '@/lib/api/types'
import { useNow, when } from '@/lib/format'
import { compareReply } from '@/lib/reply-diff'
import { ACTION_STATUS, actionLabel, emailKind } from '@/lib/vocab'
import { cn } from '@/lib/utils'

/** The reply as it stands now: the held reply (with any edits), what was posted, or what the agent wrote. */
export function currentReply(d: DecisionDetail): string | null {
  return d.approval?.reply_masked ?? d.actual_reply ?? d.reply_text ?? null
}

export function RecommendationPanel({ decision: d, vocab }: { decision: DecisionDetail; vocab?: DeskVocab }) {
  const [compare, setCompare] = React.useState(false)
  const switchId = React.useId()
  const now = useNow()
  const reply = currentReply(d)
  const standard = d.template_reply
  const { parts, same } = compareReply(reply ?? '', standard)
  const added = parts.filter((p) => p.added).length
  const action = d.final_action

  return (
    <Card className="min-w-0 flex-[1_1_440px] self-start">
      <CardHeader>
        <StepEyebrow n={3}>What it recommends</StepEyebrow>
        <h2 className="m-0 text-base font-semibold">Agent recommendation</h2>
      </CardHeader>

      {!action ? (
        <CardContent className="flex flex-col items-start gap-2">
          <span aria-hidden className="flex size-9 items-center justify-center rounded-lg bg-bad-bg text-bad-fg">
            <UserRound className="size-[18px]" />
          </span>
          <strong className="font-semibold">No recommendation — needs a person</strong>
          <p className="m-0 text-[13px] text-secondary-foreground">{d.error ? `The agent stopped: ${d.error}` : 'The agent did not reach a decision.'} Nothing was posted to the branch.</p>
        </CardContent>
      ) : (
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <span className="text-xs text-muted-foreground">Final action</span>
            <div className="flex flex-wrap items-center gap-2.5">
              <StatusPill size="lg" status={ACTION_STATUS[action]} label={actionLabel(action)} />
              {d.guardrail_changed ? (
                <span className="text-[13px] text-muted-foreground">
                  the agent proposed <s>{actionLabel(d.proposed_action)}</s>
                </span>
              ) : null}
            </div>
            {d.guardrail_changed ? <span className="text-xs text-warn-fg">Changed by a rule — see “What the rules said” below.</span> : null}
          </div>

          <div className="flex flex-col gap-1.5">
            <span className="text-xs text-muted-foreground">Confidence</span>
            <Confidence value={d.confidence} />
            {d.category === 'rules_fallback' ? <span className="text-xs text-warn-fg">Decided by the rules: the model was not reachable.</span> : null}
          </div>

          {d.reason ? (
            <div className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Why</span>
              <p className="m-0 text-[13px]">{d.reason}</p>
            </div>
          ) : null}

          {reply ? (
            <div className="flex flex-col gap-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[13px] font-semibold">
                  {d.approval?.edited ? 'Reply to branch, as edited' : 'Reply to branch'}
                  <span className="font-normal text-muted-foreground">{action === 'reply_and_close' ? ' · then the ticket closes' : action === 'request_info' ? ' · ticket stays open' : ''}</span>
                </span>
                {standard ? (
                  <div className="flex items-center gap-2">
                    <Switch id={switchId} checked={compare} onCheckedChange={setCompare} />
                    <label htmlFor={switchId} className="text-[13px]">
                      Compare with standard wording
                    </label>
                  </div>
                ) : null}
              </div>

              <div className="flex flex-wrap gap-3">
                <div className="flex min-w-0 flex-[1_1_260px] flex-col gap-1.5">
                  {compare ? <span className="text-xs font-semibold text-muted-foreground">Agent reply</span> : null}
                  <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-white px-3.5 py-3 text-[13px]">
                    <p className="m-0">
                      {parts.map((p, i) => (
                        <React.Fragment key={i}>
                          <span className={cn(compare && p.added && 'rounded bg-warn-mark px-1 py-0.5')}>{p.text}</span>{' '}
                        </React.Fragment>
                      ))}
                    </p>
                  </div>
                </div>
                {compare && standard ? (
                  <div className="flex min-w-0 flex-[1_1_220px] flex-col gap-1.5">
                    <span className="text-xs font-semibold text-muted-foreground">{vocab?.template_label ?? 'Standard wording for this outcome'}</span>
                    <div className="rounded-lg border border-dashed border-input px-3.5 py-3 text-[13px] text-secondary-foreground">{standard}</div>
                  </div>
                ) : null}
              </div>

              {compare && standard ? (
                <p className="m-0 rounded-lg bg-warn-panel px-3.5 py-2.5 text-xs text-warn-fg">
                  {same
                    ? 'The reply is the standard wording, word for word.'
                    : added
                      ? `Highlighted: ${added} sentence${added > 1 ? 's' : ''} not in the standard wording.`
                      : 'Same sentences as the standard wording, in a different order or form.'}
                </p>
              ) : null}
            </div>
          ) : (
            <p className="m-0 text-[13px] text-muted-foreground">
              {action === 'escalate' ? 'Escalations go to staff as an internal note; there is no reply to the branch.' : 'No reply text for this decision.'}
            </p>
          )}

          {d.notifications?.length ? (
            <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
              {d.notifications.map((n) => (
                <li key={n.id} className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <Mail aria-hidden className="size-3.5" />
                  {emailKind(n.kind)} email to <span className="font-mono">{n.recipient ?? '—'}</span>
                  <StatusPill size="sm" status={n.status === 'sent' ? 'sent' : n.status === 'failed' ? 'sendFailed' : n.status === 'pending' ? 'queued' : 'sendSkipped'} />
                  {n.sent_at ? <span>{when(n.sent_at, now)}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
        </CardContent>
      )}
    </Card>
  )
}
