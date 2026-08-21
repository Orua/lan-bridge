import React from 'react';
import { useApp } from '../App';

const About: React.FC = () => {
  const { tl } = useApp();

  return (
    <div className="page">
      <h2>{tl('about.title')} LAN BRIDGE</h2>

      <div className="about-card">
        <div className="about-logo">&#9653;</div>
        <h3>LAN BRIDGE</h3>
        <p className="version">v0.1.0</p>
        <p className="about-desc">{tl('about.desc')}</p>

        <div className="about-features">
          <div className="feature">
            <strong>{tl('about.f1Title')}</strong>
            <p>{tl('about.f1Desc')}</p>
          </div>
          <div className="feature">
            <strong>{tl('about.f2Title')}</strong>
            <p>{tl('about.f2Desc')}</p>
          </div>
          <div className="feature">
            <strong>{tl('about.f3Title')}</strong>
            <p>{tl('about.f3Desc')}</p>
          </div>
          <div className="feature">
            <strong>{tl('about.f4Title')}</strong>
            <p>{tl('about.f4Desc')}</p>
          </div>
        </div>

        <div className="about-links">
          <a href="#" onClick={(e) => {
            e.preventDefault();
            window.electronAPI?.openExternal('https://github.com/Orua/lan-bridge');
          }}>
            GitHub
          </a>
        </div>

        <p className="muted" style={{ marginTop: 16 }}>
          License: MIT | Built with Electron + React + FastAPI
        </p>
      </div>
    </div>
  );
};

export default About;
