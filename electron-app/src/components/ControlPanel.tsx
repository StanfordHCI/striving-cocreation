import ExclusionSettingsPanel from './ExclusionSettingsPanel';
import Modal from './Modal';

type ControlPanelProps = {
  isOpen?: boolean;
  onClose?: () => void;
};

export default function ControlPanel({ isOpen, onClose }: ControlPanelProps) {
  const panel = <ExclusionSettingsPanel />;
  if (isOpen !== undefined && onClose) {
    return (
      <Modal isOpen={isOpen} onClose={onClose} className="settings-modal" title="Privacy settings">
        {panel}
      </Modal>
    );
  }
  return panel;
}
