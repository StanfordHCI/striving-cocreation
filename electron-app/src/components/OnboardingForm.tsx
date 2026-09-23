import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowUpRight } from 'lucide-react';
import api, { TempoApiError } from '../api/client';
import type { OnboardingQuestion } from '../api/contracts';
import Modal from './Modal';
import './OnboardingForm.css';

const SECTIONS = [
  { id: 'everyday', title: 'Roles and daily routine', keys: ['roles', 'typical_day', 'main_concerns'] },
  { id: 'direction', title: 'Work, learning, and personal goals', keys: ['work', 'personal_growth', 'education'] },
  { id: 'support', title: 'Relationships, health, and finances', keys: ['relationships', 'health', 'finances'] },
  { id: 'changes', title: 'Stress and recent changes', keys: ['stressors', 'recent_changes', 'additional_context'] },
];

/** Optional context. Back and Cancel never save answers or start recording. */
export default function OnboardingForm({
  mode, onBack, onDone, onSkip,
}: {
  mode: 'edit' | 'gate';
  onBack: () => void;
  onDone: () => void;
  onSkip: () => void;
}) {
  const [questions, setQuestions] = useState<OnboardingQuestion[]>([]);
  const [userName, setUserName] = useState('');
  const [responses, setResponses] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    api.getOnboarding(controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        setQuestions(data.questions);
        setUserName(data.user_name);
        setResponses(data.responses);
        setLoaded(true);
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : 'Could not load the questions');
        setLoading(false);
      });
    return () => controller.abort();
  }, []);

  async function skip() {
    if (submitting.current) return;
    submitting.current = true;
    setSaving(true);
    try {
      // Do not overwrite existing answers if loading them failed.
      if (loaded) await api.saveOnboarding({ user_name: userName.trim(), responses, dismissed: true });
    } catch {
      // A failed dismissal must not prevent the explicit recording action.
    }
    onSkip();
  }

  async function save() {
    if (!loaded || submitting.current) return;
    submitting.current = true;
    setSaving(true);
    setError(null);
    try {
      await api.saveOnboarding({ user_name: userName.trim(), responses });
      onDone();
    } catch (reason) {
      setError(reason instanceof TempoApiError ? reason.message : 'Could not save your answers');
      submitting.current = false;
      setSaving(false);
    }
  }

  const answered = questions.filter((question) => responses[question.key]?.trim()).length;
  const sections = SECTIONS.map((section) => ({
    ...section,
    questions: section.keys.flatMap((key) => questions.filter((question) => question.key === key)),
  })).filter((section) => section.questions.length > 0);
  const extraQuestions = questions.filter((question) => !SECTIONS.some((section) => section.keys.includes(question.key)));
  if (extraQuestions.length) {
    sections.push({ id: 'more', title: 'Other information', keys: [], questions: extraQuestions });
  }

  return (
    <Modal isOpen onClose={onBack} className="onboarding-dialog" ariaLabel="About you" closeOnBackdropClick={false} closeOnEscape={!saving}>
      <header className="onboarding-topbar">
        <button type="button" className="onboarding-back" onClick={onBack} disabled={saving}>
          <ArrowLeft size={18} aria-hidden="true" /> Back to Record
        </button>
      </header>

      <div className="onboarding-scroll">
        <div className="onboarding-layout">
          <aside className="onboarding-intro">
            <p className="eyebrow">Optional questions</p>
            <h1>About you</h1>
            <p>Tempo uses these answers to help identify the goals behind your recorded activity.</p>
            <p className="onboarding-note">{'Skip any question. You can edit your answers later.'}</p>
            <nav className="onboarding-sections" aria-label="About you sections">
              {sections.map((section, index) => (
                <a href={`#onboarding-${section.id}`} key={section.id}>
                  <span className="onboarding-section-number">0{index + 1}</span>
                  <span>{section.title}</span><ArrowUpRight size={14} aria-hidden="true" />
                </a>
              ))}
            </nav>
            <p className="onboarding-storage">Saved on this device. Your answers are included in the context sent to your chosen AI provider when Tempo interprets your activity.</p>
          </aside>

          <div className="onboarding-fields" aria-busy={loading || saving}>
            {loading && <p className="onboarding-loading" role="status">Loading your answers…</p>}
            {loaded && (
              <fieldset disabled={saving}>
                <label className="onboarding-name" htmlFor="onboarding-name">
                  <span>What should Tempo call you?</span>
                  <input id="onboarding-name" autoComplete="given-name" value={userName} onChange={(event) => setUserName(event.target.value)} placeholder="Your name (optional)" />
                </label>
                {sections.map((section, index) => (
                  <section className="onboarding-section" id={`onboarding-${section.id}`} key={section.id} aria-labelledby={`onboarding-heading-${section.id}`}>
                    <header className="onboarding-section-heading">
                      <span className="onboarding-section-number">0{index + 1}</span>
                      <h2 id={`onboarding-heading-${section.id}`}>{section.title}</h2>
                    </header>
                    {section.questions.map((question) => (
                      <label className="onboarding-question" key={question.key}>
                        <span className="onboarding-question-label">{question.label}</span>
                        <span className="onboarding-question-prompt">{question.prompt}</span>
                        <textarea
                          value={responses[question.key] ?? ''}
                          onChange={(event) => setResponses((current) => ({ ...current, [question.key]: event.target.value }))}
                          placeholder={question.placeholder}
                          rows={3}
                        />
                      </label>
                    ))}
                  </section>
                ))}
              </fieldset>
            )}
          </div>
        </div>
      </div>

      <footer className="onboarding-footer">
        {error && <p className="onboarding-error" role="alert">{error}</p>}
        <div className="onboarding-actions">
          <span className="onboarding-count" role="status">{loaded ? `${answered} of ${questions.length} answered · All optional` : 'Every question is optional'}</span>
          <div className="onboarding-actions-right">
            <button type="button" className="secondary-button" onClick={mode === 'edit' ? onBack : () => void skip()} disabled={saving || (mode === 'gate' && loading)}>
              {mode === 'edit' ? 'Cancel' : 'Skip & start recording'}
            </button>
            <button type="button" className="primary-button" onClick={() => void save()} disabled={saving || !loaded}>
              {saving ? 'Saving…' : mode === 'edit' ? 'Save answers' : 'Save & start recording'}
            </button>
          </div>
        </div>
      </footer>
    </Modal>
  );
}
