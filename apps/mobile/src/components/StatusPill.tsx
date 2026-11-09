import { StyleSheet, Text, View } from "react-native";
import type { ProcessingStatus, UploadStatus } from "../types/domain";
import { colors } from "../theme";

const labels: Record<UploadStatus | ProcessingStatus, string> = {
  saved_local: "本机已保存",
  uploading: "上传中",
  saved_cloud: "云端已保存",
  upload_failed: "上传失败",
  queued: "排队中",
  parsing: "解析中",
  organizing: "整理中",
  ready: "可使用",
  needs_input: "待补充",
  failed: "处理失败",
};

export const StatusPill = ({ status }: { status: UploadStatus | ProcessingStatus }) => {
  const isProblem = status === "upload_failed" || status === "failed";
  const isPending = status === "queued" || status === "parsing" || status === "organizing" || status === "needs_input";
  return (
    <View style={[styles.pill, isProblem && styles.problem, isPending && styles.pending]}>
      <Text style={[styles.text, isProblem && styles.problemText, isPending && styles.pendingText]}>{labels[status]}</Text>
    </View>
  );
};

const styles = StyleSheet.create({
  pill: { paddingHorizontal: 9, paddingVertical: 5, borderRadius: 999, backgroundColor: colors.primarySoft },
  text: { color: colors.primary, fontSize: 12, fontWeight: "700" },
  pending: { backgroundColor: colors.warningSoft },
  pendingText: { color: colors.warning },
  problem: { backgroundColor: colors.dangerSoft },
  problemText: { color: colors.danger },
});
