import { useMemo, useState } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { ChoiceChips } from "../components/ChoiceChips";
import { FormField } from "../components/FormField";
import { PrimaryButton } from "../components/PrimaryButton";
import { ScreenHeader } from "../components/ScreenHeader";
import { StatusPill } from "../components/StatusPill";
import { colors, spacing } from "../theme";
import type {
  ContextUpdate,
  Domain,
  ExperienceInput,
  Project,
  SourceDetail,
  VerificationResult,
  Visibility,
} from "../types/domain";

const resultLabels: Record<VerificationResult, string> = {
  untried: "未尝试",
  effective: "有效",
  partially_effective: "部分有效",
  ineffective: "无效",
};

interface Props {
  source: SourceDetail;
  domains: Domain[];
  projects: Project[];
  saving: boolean;
  error?: string | undefined;
  onBack: () => void;
  onSaveContext: (input: ContextUpdate) => Promise<void>;
  onAddExperience: (input: ExperienceInput) => Promise<void>;
}

export const DetailScreen = ({ source, domains, projects, saving, error, onBack, onSaveContext, onAddExperience }: Props) => {
  const [domainId, setDomainId] = useState(source.domainId);
  const [projectId, setProjectId] = useState(source.projectIds[0] ?? "");
  const [visibility, setVisibility] = useState<Visibility>(source.visibility);
  const [reason, setReason] = useState(source.personalContext.reason);
  const [notes, setNotes] = useState(source.personalContext.notes);
  const [applicability, setApplicability] = useState(source.personalContext.applicability);
  const [result, setResult] = useState<Exclude<VerificationResult, "untried">>("effective");
  const [experienceNotes, setExperienceNotes] = useState("");

  const availableProjects = useMemo(
    () => projects.filter((item) => !item.domainId || item.domainId === domainId),
    [domainId, projects],
  );

  const saveContext = () => onSaveContext({
    domainId,
    projectIds: projectId ? [projectId] : [],
    visibility,
    reason: reason.trim(),
    notes: notes.trim(),
    applicability: applicability.trim(),
    version: source.personalContext.version,
  });

  const addExperience = async () => {
    if (!experienceNotes.trim()) return;
    await onAddExperience({
      result,
      notes: experienceNotes.trim(),
      ...(projectId ? { projectId } : {}),
    });
    setExperienceNotes("");
  };

  return (
    <View style={styles.screen}>
      <ScreenHeader title={source.title} subtitle="资料详情" onBack={onBack} />
      <ScrollView contentContainerStyle={styles.content}>
        {error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null}
        <View style={styles.statusRow}>
          <StatusPill status={source.uploadStatus} />
          <StatusPill status={source.processingStatus} />
          <View style={styles.neutralPill}><Text style={styles.neutralText}>{coverageLabel(source.coverage)}</Text></View>
        </View>

        <Section title="保存内容">
          {source.rawUrl ? <Text selectable style={styles.link}>{source.rawUrl}</Text> : null}
          <Text selectable style={styles.bodyText}>{source.extractedText || source.rawInput || "当前只有链接或原始输入，尚无可展示正文。"}</Text>
        </Section>

        <Section title="个人上下文" subtitle="这些内容由你确认，不会被自动整理覆盖。">
          <Text style={styles.label}>知识域</Text>
          <ChoiceChips accessibilityLabel="修改知识域" choices={domains.map((item) => ({ id: item.id, label: item.name }))} selectedId={domainId} onSelect={(id) => { setDomainId(id); setProjectId(""); }} />
          <Text style={styles.label}>关联项目</Text>
          <ChoiceChips accessibilityLabel="修改项目" choices={availableProjects.map((item) => ({ id: item.id, label: item.name }))} selectedId={projectId} allowClear onSelect={setProjectId} />
          <Text style={styles.label}>Agent 可见性</Text>
          <ChoiceChips
            accessibilityLabel="修改 Agent 可见性"
            choices={[{ id: "app_and_agents", label: "按知识域授权" }, { id: "app_only", label: "仅 App 可见" }]}
            selectedId={visibility}
            onSelect={(id) => setVisibility(id as Visibility)}
          />
          <FormField label="为什么保存" value={reason} onChangeText={setReason} multiline />
          <FormField label="个人备注" value={notes} onChangeText={setNotes} multiline />
          <FormField label="适用条件" value={applicability} onChangeText={setApplicability} multiline />
          <PrimaryButton label="保存个人上下文" onPress={() => void saveContext()} busy={saving} />
        </Section>

        <Section title="实践记录" subtitle="记录一次实际使用结果；历史记录会保留。">
          {source.experiences.length ? source.experiences.map((experience) => (
            <View key={experience.id} style={styles.experience}>
              <Text style={styles.experienceTitle}>{resultLabels[experience.result]} · {formatDate(experience.verifiedAt)}</Text>
              <Text style={styles.bodyText}>{experience.notes || "未填写说明"}</Text>
            </View>
          )) : <Text style={styles.muted}>尚无实践记录，当前状态为未尝试。</Text>}
          <ChoiceChips
            accessibilityLabel="选择验证结果"
            choices={[
              { id: "effective", label: "有效" },
              { id: "partially_effective", label: "部分有效" },
              { id: "ineffective", label: "无效" },
            ]}
            selectedId={result}
            onSelect={(id) => setResult(id as Exclude<VerificationResult, "untried">)}
          />
          <FormField label="本次使用说明" hint="请写下效果、限制或失败原因。" value={experienceNotes} onChangeText={setExperienceNotes} multiline />
          <PrimaryButton label="追加实践记录" onPress={() => void addExperience()} busy={saving} disabled={!experienceNotes.trim()} variant="secondary" />
        </Section>
      </ScrollView>
    </View>
  );
};

const Section = ({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) => (
  <View style={styles.section}>
    <Text style={styles.sectionTitle}>{title}</Text>
    {subtitle ? <Text style={styles.muted}>{subtitle}</Text> : null}
    {children}
  </View>
);

const coverageLabel = (coverage: SourceDetail["coverage"]): string => ({
  link_only: "仅链接",
  partial_text: "部分文本",
  full_text: "已提取正文",
})[coverage];

const formatDate = (value: string): string => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("zh-CN");
};

const styles = StyleSheet.create({
  screen: { flex: 1 },
  content: { padding: spacing.md, paddingBottom: spacing.xl, gap: spacing.md },
  statusRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs },
  neutralPill: { paddingHorizontal: 9, paddingVertical: 5, borderRadius: 999, backgroundColor: colors.surfaceMuted },
  neutralText: { color: colors.textMuted, fontSize: 12, fontWeight: "700" },
  section: { padding: spacing.md, borderRadius: 18, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.surface, gap: spacing.sm },
  sectionTitle: { color: colors.text, fontSize: 18, fontWeight: "800" },
  label: { color: colors.text, fontSize: 15, fontWeight: "700", marginTop: spacing.xs },
  bodyText: { color: colors.text, fontSize: 15, lineHeight: 23 },
  link: { color: colors.primary, fontSize: 14, lineHeight: 21 },
  muted: { color: colors.textMuted, fontSize: 13, lineHeight: 19 },
  experience: { padding: spacing.sm, borderRadius: 12, backgroundColor: colors.surfaceMuted, gap: spacing.xs },
  experienceTitle: { color: colors.text, fontWeight: "700" },
  error: { padding: spacing.sm, color: colors.danger, backgroundColor: colors.dangerSoft, borderRadius: 10 },
});
