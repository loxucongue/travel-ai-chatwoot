import { Construction } from 'lucide-react';
import { PageHeader } from '../components';

export default function Unavailable({ title, description }: { title: string; description: string }) {
  return <div className="page-content"><PageHeader title={title} description={description} /><section className="panel unavailable-panel"><Construction size={30} /><h2>本阶段尚未开放</h2><p>当前页面已停止展示原型 Mock 数据。完成自动回复闭环后再接入真实统计和业务操作。</p></section></div>;
}

